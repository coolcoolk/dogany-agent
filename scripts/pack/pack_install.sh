#!/bin/bash
# pack_install.sh -- post-mint pack integration installer (manifest-driven).
#
# Runs the FULL deterministic chain for a named pack after the framework
# mint is complete. Every step is idempotent. Failure stops immediately
# with nonzero exit and the last log line as the error surface.
#
# DGN-366 L1/L2 generalization: the installer is pack-agnostic. Each pack
# ships a pack-manifest.json next to its payload declaring its categories,
# reference identity (slug/root) and idempotency markers. The installer
# preflights and installs ONLY the declared categories -- no hard
# requirements on lib/, knowledge-snapshot.sh or ledger.py remain.
#
# Usage:
#   pack_install.sh <slug> <root> --pack <pack-id>
#                   [--source-tag <pack/<id>/vX.Y.Z[-pre]>]
#                   [--untagged-local <reason>]
#                   [--instance-root <path>] [--catalog <file>]
#                   [--model <sonnet|opus|haiku>]
#                   [--migrate-from <peer-root>] [--no-start] [--no-state]
#                   [--dry-run]
#
# Arguments:
#   <slug>          agent slug (kebab-case, must match the minted instance)
#   <root>          absolute path to the minted instance root
#   --pack <id>     pack id from the catalog (required)
#   --source-tag <ref>
#                   the pack tag this payload was actually taken from
#                   (dec-120 #1 grammar: pack/<id>/v<version>). The
#                   DOGANY_PACKS ledger records THIS ref's version segment,
#                   not the manifest's pack_version -- so a pre-release
#                   install (v0.11.0-dev.1 over a manifest that still says
#                   0.11.0) stays distinguishable in the ledger from the
#                   later stable cut. The version segment must equal the
#                   resolved pack_version or be a pre-release/build-suffixed
#                   form of it; anything else is a manifest==tag 3-point
#                   violation and fails closed. OMITTED (default) = the
#                   ledger records the resolved pack_version -- but when the
#                   pack source is a git work tree, the source-tag gate
#                   (DGN-1489, below the D1 gate) then requires the tag
#                   pack/<id>/v<resolved version> to EXIST, or an explicit
#                   --untagged-local declaration. Non-git sources (test
#                   fixtures, tar-extraction lane) are out of that gate's
#                   scope and stay byte-identical to the pre-flag behavior.
#   --untagged-local <reason>
#                   explicit opt-in for a git-work-tree pack source whose
#                   resolved version has NO tag (deliberate local/dev
#                   install). The reason is mandatory, logged, and recorded
#                   in the install receipt (source=untagged-local:<reason>)
#                   so the ledger's tag-backing can always be audited.
#                   Mutually exclusive with --source-tag (a tag-backed
#                   install is by definition not untagged).
#   --instance-root <path>
#                   root of the CALLING instance (the agent running the
#                   mint). Instance-dependent steps (minting_state record)
#                   resolve against it. When absent those steps SKIP with
#                   an explicit log line (never silently).
#   --catalog <file>
#                   catalog file override (default: <repo>/packs/catalog.json
#                   relative to this script). Relative package_dir entries
#                   resolve against the catalog file's directory.
#   --model <m>     requested model for the instance (default: sonnet;
#                   used by step 9 verify)
#   --migrate-from <peer-root>
#                   MIGRATION PATH (DGN-284 #2/#3/#6): this mint migrates an
#                   existing user's records from the main-agent instance at
#                   <peer-root>. Effects: peer integration keys (L1_DB /
#                   L1_EXPECTED_USER_VERSION / HANDOFF_PEER_AG) are appended
#                   to agent.conf pointing at <peer-root>, and the domain
#                   seed (when declared) is 'pending_data'. OMITTED
#                   (default) = fresh/standalone mint: NO peer keys, domain
#                   seed 'ready', no migration deferral.
#   --no-start      compatibility no-op: this installer never starts launchd;
#                   the caller owns `mint_run.sh start`.
#   --no-state      skip step 11 minting_state record (safe for tests)
#   --dry-run       resolve plan + preflight; no writes, exit 0 = plan OK
#
# pack-manifest.json contract (lives at <package_dir>/pack-manifest.json):
#   {
#     "name": "<pack name>",
#     "reference_slug": "<slug the payload was authored for; ALSO the
#                         payload subdirectory name under package_dir>",
#     "reference_root": "<absolute instance root the payload was authored
#                         for; rendered to the minted root at install>",
#     "reference_home": "<optional: home prefix of reference_root; enables
#                         tilde-form and home-prefix rendering>",
#     "agent_md_marker": "<idempotency marker inside AGENT.md.add>",
#     "agent_conf_marker": "<idempotency marker line for agent.conf.add>",
#     "domain_seed": true|false (optional; step 8 runs only when truthy),
#     "categories": [ {"category": "<name>", "required": true|false,
#                      "files": [...]  (optional, 'lib' only)} ... ],
#     "knowledge": { ... }  (optional; REQUIRED together with the
#                      knowledge_snapshot category -- half declaration is a
#                      preflight FAIL. DGN-402 knowledge wiring standard;
#                      schema + authoring rules: docs/KNOWLEDGE-WIRING.md)
#   }
#   Category names: lib, routines, plists, prompts, agent_conf_fragment,
#   triggers, db_migrations, skills, agent_md_fragment, scripts,
#   knowledge_snapshot.
#
# Steps (all idempotent, all logged to <root>/.telegram_bot/logs/pack-install.log):
#   0. D1 commit gate: the resolved pack_version must have a commit
#      on some ref of the pack source's repository, or the install is refused
#      -- including on --dry-run. No override flag. Rule + measurement:
#      lib/pack_commit_gate.sh.
#   1. preflight checks (declared categories only)
#   2. package copy (declared categories: lib/ routines/ prompts/ plists/
#      db_migrations/ scripts/ triggers); plists are RENDERED, not copied:
#      launchd labels, plist filenames and reference paths are derived from
#      <slug>/<root> (DGN-284 #1). A bundled plists.defer is MERGE-APPENDED
#      into the instance framework defer (DGN-227 MINOR-5), not clobbered.
#   3. agent.conf fragment append (idempotent via manifest marker; peer
#      integration keys only on the migration path -- DGN-284 #3/#6)
#   4. W01 ledger apply CLI (only when the pack ships lib/ledger.py)
#   5. declared hook wiring (only when the manifest declares hooks[];
#      the installer wires exactly what is declared -- it knows neither
#      filename nor hook event, DGN-1496)
#   6. knowledge snapshot (only when knowledge_snapshot declared; source
#      resolves to the bundled frozen snapshot at
#      <package_dir>/<reference_slug>/knowledge/<warehouse>/ when present
#      (DGN-227 B5 delivery channel), else falls back to manifest
#      knowledge.source publisher-local path -- DGN-402)
#   7. skills install, two modes: REFINE (instance bundle dir exists -> render
#      SKILL.md only, DGN-402) and NET-NEW (bundle dir absent -> install the
#      whole payload skill directory, text rendered / binaries copied,
#      DGN-227 B6). Both preserve-register pack-owned and reconcile against
#      the install ledger (D1)
#   7b. AGENT.md fragment append (RENDERED via the same slug/root
#       substitution as plists; idempotent via manifest marker)
#   7c. knowledge wiring selftest (knowledge_selftest.sh: gates G1-G4 for
#       warehouse packs, inverse check for warehouse-less packs; zero-model,
#       exit != 0 = install FAIL -- DGN-402)
#   8. domain seed (only when manifest declares domain_seed; migration
#      path = pending_data, fresh = ready; DGN-284 #2, decision 11)
#   9. model config verify (settings.json model == requested model)
#  11. minting_state record via <instance-root> [--no-state; skips with an
#      explicit log line when --instance-root is absent]
#  (+) deps-provision (DGN-850): payload requirements.txt -> instance runtime
#      interpreters via pack_deps_provision.sh; runs right before the
#      DOGANY_PACKS upsert on BOTH the kit and agent/module paths. Zero-delta
#      when the pack ships no requirements.txt; a provisioning failure never
#      fails the install (consumers keep their own fallbacks).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
# DGN-1406/1409: absolute self-path, used by the requires_kit auto-resolve
# seam to re-invoke this exact script for a one-hop kit dependency install.
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
DEFAULT_CATALOG="$REPO_DIR/packs/catalog.json"

# DGN-1572: launchd unit install directory, overridable so a sandboxed test
# instance can never register a real unit in the owner's live launchd domain.
# Unset (the live default) resolves to the exact historical path.
LAUNCH_AGENTS_DIR="${DOGANY_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"

# DGN-1424: resolve the python3 interpreter ONCE, PATH-independent (a bare
# `python3` call resolves whatever the CALLER's PATH hands back, which on a
# human's own terminal can be macOS's bundled 3.9.6 -- see the lib header).
# Exported so every subprocess this installer spawns (compat-lint.sh,
# mint_run.sh, knowledge_selftest.sh, pack_deps_provision.sh, ...) reuses the
# SAME resolution instead of re-probing PATH on its own.
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

# DGN-1845: every whole-file write into the live instance tree goes through
# a temp sibling + rename (a live turn must never read a half-written module).
ATOMIC_WRITE_LIB="$SCRIPT_DIR/lib/atomic_write.sh"
[[ -f "$ATOMIC_WRITE_LIB" && -r "$ATOMIC_WRITE_LIB" ]] \
  || { echo "ERROR: atomic write library missing ($ATOMIC_WRITE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/atomic_write.sh
. "$ATOMIC_WRITE_LIB"

# DGN-773 R5/T4b: persona write-target resolver (PROFILE.md vs AGENT.md).
PERSONA_LIB="$SCRIPT_DIR/lib/persona_resolver.sh"
[[ -f "$PERSONA_LIB" && -r "$PERSONA_LIB" ]] \
  || { echo "ERROR: persona resolver library missing ($PERSONA_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/persona_resolver.sh
. "$PERSONA_LIB"

# DGN-1079 RR1: pack coordinate resolver (catalog package_dir -- single rule,
# shared with pack_publish.sh so the publisher cannot construct a rival one).
COORDS_LIB="$SCRIPT_DIR/lib/pack_coords.sh"
[[ -f "$COORDS_LIB" && -r "$COORDS_LIB" ]] \
  || { echo "ERROR: pack coordinate library missing ($COORDS_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/pack_coords.sh
. "$COORDS_LIB"

# D1 commit gate ("the version this install carries must have a commit").
# Rule owner = the d1-gate-build.md build report under the repo report tree
# (no ticket number minted). Cited by basename on purpose: the DGN-1123
# arm-state gate forbids this installer from spelling any agent-data path,
# comments included.
# Shared with pack_publish.sh so the publisher prints the exact commit the
# installer will demand -- one rule, both ends of the pack door.
COMMIT_GATE_LIB="$SCRIPT_DIR/lib/pack_commit_gate.sh"
[[ -f "$COMMIT_GATE_LIB" && -r "$COMMIT_GATE_LIB" ]] \
  || { echo "ERROR: pack commit gate library missing ($COMMIT_GATE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/pack_commit_gate.sh
. "$COMMIT_GATE_LIB"

# DGN-1018 RR2: kit/service-namespace token grammar DATA (regex core +
# framework-reserved name list), shared with compat-lint.sh's independent
# duplicate validation -- see the lib file header for why this is data-only.
TOKEN_GRAMMAR_LIB="$SCRIPT_DIR/lib/pack_token_grammar.sh"
[[ -f "$TOKEN_GRAMMAR_LIB" && -r "$TOKEN_GRAMMAR_LIB" ]] \
  || { echo "ERROR: token grammar library missing ($TOKEN_GRAMMAR_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/pack_token_grammar.sh
. "$TOKEN_GRAMMAR_LIB"

# DGN-1395: retention policy for the ".prev-<stamp>" shared-skill backups this
# installer creates. The predicate lives in the lib so the write-time pruner
# below and the operator's one-time reconcile cannot drift apart.
PREV_PRUNE_LIB="$SCRIPT_DIR/lib/prev_backup_prune.sh"
[[ -f "$PREV_PRUNE_LIB" && -r "$PREV_PRUNE_LIB" ]] \
  || { echo "ERROR: prev-backup prune library missing ($PREV_PRUNE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/prev_backup_prune.sh
. "$PREV_PRUNE_LIB"

# DGN-1422: declaration-vs-reality skill retire (the DGN-1409 counterpart --
# a skill DROPPED from a newer manifest must stop being active, not just a
# skill newly ADDED). Predicate + archive/restore all live in the lib so the
# dry-run preview and the real removal cannot drift apart (same doctrine as
# prev_backup_prune.sh above).
SKILL_RETIRE_LIB="$SCRIPT_DIR/lib/skill_retire.sh"
[[ -f "$SKILL_RETIRE_LIB" && -r "$SKILL_RETIRE_LIB" ]] \
  || { echo "ERROR: skill retire library missing ($SKILL_RETIRE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/skill_retire.sh
. "$SKILL_RETIRE_LIB"

# DGN-1456/1460: install completion/source evidence contract. This script is
# the only WRITER (evidence_begin before the first payload byte,
# evidence_complete before the DOGANY_PACKS projection); the requires_kit
# gate below and update_plan/update_apply are the CONSUMERS -- all through
# the same lib, so install and update read the same fact.
INSTALL_EVIDENCE_LIB="$SCRIPT_DIR/lib/install_evidence.sh"
[[ -f "$INSTALL_EVIDENCE_LIB" && -r "$INSTALL_EVIDENCE_LIB" ]] \
  || { echo "ERROR: install evidence library missing ($INSTALL_EVIDENCE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/install_evidence.sh
. "$INSTALL_EVIDENCE_LIB"

# DGN-1491: baseline-tag resolver for the content-drift guard, shared with
# update.sh (extracted there from the DGN-1201 inline function) -- one ladder,
# two callers, never fork (DGN-1034 shape guard).
DRIFT_BASELINE_LIB="$SCRIPT_DIR/lib/drift_baseline.sh"
[[ -f "$DRIFT_BASELINE_LIB" && -r "$DRIFT_BASELINE_LIB" ]] \
  || { echo "ERROR: drift baseline library missing ($DRIFT_BASELINE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/drift_baseline.sh
. "$DRIFT_BASELINE_LIB"

# ---------- arg parse -------------------------------------------------------
SLUG="${1:-}"
ROOT="${2:-}"
PACK_ID="" DRY=0 NO_START=0 NO_STATE=0 MODEL_OPT="" MIGRATION=0 PEER_ROOT=""
INSTANCE_ROOT="" CATALOG="$DEFAULT_CATALOG" CATALOG_EXPLICIT=0 UPGRADE=0 PACK_DIR_OVERRIDE=""
SOURCE_TAG="" UNTAGGED_REASON="" PREPARE=0
shift 2 2>/dev/null || { echo "usage: pack_install.sh <slug> <root> --pack <id> [--pack-dir <dir>] [--source-tag <pack/<id>/vX.Y.Z[-pre]>] [--untagged-local <reason>] [--instance-root <path>] [--catalog <file>] [--model <sonnet|opus|haiku>] [--migrate-from <peer-root>] [--upgrade] [--prepare] [--no-start] [--no-state] [--dry-run]" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack)          PACK_ID="$2"; shift 2 ;;
    --pack-dir)      PACK_DIR_OVERRIDE="$2"; shift 2 ;;
    --source-tag)    SOURCE_TAG="$2"; shift 2 ;;
    --untagged-local) UNTAGGED_REASON="$2"; shift 2 ;;
    --instance-root) INSTANCE_ROOT="$2"; shift 2 ;;
    --catalog)       CATALOG="$2"; CATALOG_EXPLICIT=1; shift 2 ;;
    --model)         MODEL_OPT="$2"; shift 2 ;;
    --migrate-from)  PEER_ROOT="$2"; MIGRATION=1; shift 2 ;;
    --upgrade)       UPGRADE=1; shift ;;
    --prepare)       PREPARE=1; shift ;;
    --no-start)      NO_START=1; shift ;;
    --no-state)      NO_STATE=1; shift ;;
    --dry-run|--dry) DRY=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
MODEL_OPT="${MODEL_OPT:-sonnet}"

[[ -n "$SLUG" ]] || { echo "ERROR: slug required" >&2; exit 1; }
[[ -n "$ROOT" ]] || { echo "ERROR: root required" >&2; exit 1; }
[[ -n "$PACK_ID" ]] || { echo "ERROR: --pack <id> required" >&2; exit 1; }
# DGN-1698: direct installs on the mint/crew-join path must use the same
# canonical-coordinate default as mint_run.sh/update_seed.sh.  An explicit
# --catalog remains authoritative.
if [[ "$CATALOG_EXPLICIT" -eq 0 ]]; then
  CANONICAL_CATALOG_LIB="$SCRIPT_DIR/lib/canonical_catalog.sh"
  [[ -f "$CANONICAL_CATALOG_LIB" && -r "$CANONICAL_CATALOG_LIB" ]] \
    || { echo "ERROR: canonical catalog library missing ($CANONICAL_CATALOG_LIB) -- fail-closed" >&2; exit 1; }
  # shellcheck source=lib/canonical_catalog.sh
  . "$CANONICAL_CATALOG_LIB"
  CATALOG="$(pack_default_catalog_for_instance "$REPO_DIR" "$INSTANCE_ROOT" "pack_install")" || exit 1
fi
# DGN-1489: the two source declarations contradict each other -- a tag-backed
# install is by definition not untagged. Refused at parse time, before any
# gate output, so a caller scripting both learns immediately.
if [[ -n "$SOURCE_TAG" && -n "$UNTAGGED_REASON" ]]; then
  echo "ERROR: --source-tag and --untagged-local are contradictory declarations -- pass exactly one (DGN-1489)" >&2
  exit 1
fi
# DGN-1509 S5: --prepare stages a candidate generation under database/.gen/
# named by the candidate tag -- it needs a tag to name the directory, and
# --upgrade is a different (in-place) verb; the two are refused together so
# a caller cannot ask for both write shapes in one invocation.
if [[ "$PREPARE" -eq 1 ]]; then
  [[ -n "$SOURCE_TAG" ]] || { echo "ERROR: --prepare requires --source-tag <pack/<id>/vX.Y.Z[-pre]> (the candidate generation's directory name)" >&2; exit 1; }
  [[ "$UPGRADE" -eq 0 ]] || { echo "ERROR: --prepare and --upgrade are contradictory (prepare stages a generation; upgrade installs in place) -- pass exactly one" >&2; exit 1; }
fi
if [[ "$MIGRATION" -eq 1 ]]; then
  [[ -n "$PEER_ROOT" ]] || { echo "ERROR: --migrate-from requires a peer root path" >&2; exit 1; }
fi

# --pack-dir overrides catalog lookup (used for kit-class packs that may not
# be in the local catalog yet). The directory must contain pack-manifest.json.
if [[ -n "$PACK_DIR_OVERRIDE" ]]; then
  [[ -d "$PACK_DIR_OVERRIDE" ]] || { echo "ERROR: --pack-dir not a directory: $PACK_DIR_OVERRIDE" >&2; exit 1; }
  [[ -f "$PACK_DIR_OVERRIDE/pack-manifest.json" ]] || { echo "ERROR: --pack-dir missing pack-manifest.json: $PACK_DIR_OVERRIDE" >&2; exit 1; }
else
  [[ -f "$CATALOG" ]] || { echo "ERROR: catalog not found: $CATALOG" >&2; exit 1; }
fi

# ---------- logging ---------------------------------------------------------
LOG_DIR="$ROOT/.telegram_bot/logs"
LOG_FILE="$LOG_DIR/pack-install.log"

_log() {
  local ts msg
  ts="$(date '+%Y-%m-%dT%H:%M:%S')"
  msg="$1"
  if [[ "$DRY" -eq 1 ]]; then
    echo "[dry-run] $msg" >&2
  elif [[ -d "$LOG_DIR" ]]; then
    echo "[$ts] $msg" | tee -a "$LOG_FILE"
  else
    # log dir not created yet (pre-preflight) -- stdout only
    echo "[$ts] $msg"
  fi
}

_fail() {
  _log "FATAL: $1"
  exit 1
}

# _gate_stop <gate-id> <operator reason...> -- DGN-1506 item 3.
# A PLANNED, user-reachable stop (a designed step of a valid flow, not a
# fault) fires through here instead of _fail. Same log + rc=1, plus: when the
# caller set DOGANY_GATE_STATE_FILE (mint_run.sh pipeline does; a bare
# operator invocation does not), record WHICH gate fired as a machine fact.
# The pipeline reads that marker to send the gate's user_hint (registry:
# gates.json) through the leader's push.sh -- the sentence the user reads
# never passes through the model, and never through log-text parsing.
# Registered ids are cross-checked against gates.json by
# scripts/user-gate-lint.py at the publish doors (drift either way FAILs).
_gate_stop() {
  local gid="$1"; shift
  if [[ -n "${DOGANY_GATE_STATE_FILE:-}" ]]; then
    printf 'gate_id=%s\noperator_reason=%s\n' "$gid" "$*" \
      > "$DOGANY_GATE_STATE_FILE" 2>/dev/null \
      || _log "WARN: gate marker unwritable ($DOGANY_GATE_STATE_FILE) -- stop proceeds, hint lane degrades to mint.fault"
  fi
  _fail "$*"
}

# ---------- NM3: payload checksum verification GATE (shared) -----------------
# _nm3_verify <package_dir> [<payload_subdir>]
#
# DGN-227 B4-5 / D2: before applying ANY payload, verify the shipped bytes
# against the pack's SEAL. Since DGN-1549 the gate is LANE-SPLIT on what the
# seal physically is:
#
#   git work-tree source  -> the seal is the GIT OBJECT STORE: the verified
#     pack tag (source-tag gate above) names a commit names a tree, and the
#     install must ship exactly that tree's bytes. A checksums.sha file at a
#     git coordinate is NEVER consulted -- that file was the DGN-1549 seam:
#     written once at the 2026-08-25 scaffold import, refreshed by nothing,
#     and read as the seal authority, it published dev pack 1.3.1 AND 1.4.0
#     in an uninstallable state that stayed invisible while consumers were
#     zero. The gate here verifies (1) tracked bytes+modes == the tag's tree
#     for the whole package subtree (git diff) and (2) no untracked/ignored
#     file hides under the payload subtree that would install unsealed
#     (git status --ignored). It can never go stale: the reference is the
#     tag tree itself, not a projection of it.
#     Stated boundary (measured 2026-09-18, all three catalog repos): no
#     content filter (autocrlf/.gitattributes), no symlink, no submodule, no
#     sparse checkout exists in the estate's pack repos. git diff compares
#     post-clean-filter, so a filter-mediated checkout could pass while disk
#     bytes differ from blob bytes -- if a pack repo ever grows filters,
#     this gate needs a byte-level lane first.
#
#   non-git source (tag-archive extraction, test fixture, plain export) ->
#     verify each shipped file against checksums.sha (sha256). A mismatch or
#     a listed file missing on disk = FAIL loudly (never warn-continue) --
#     and the mismatch message names BOTH possible truths: corrupted payload,
#     or a seal that was already stale when the version was cut (the
#     DGN-1549 shape; such a tag is permanently defective, re-cut it).
#     checksums.sha lines are '<sha256hex>  <relpath>' with relpath relative
#     to <package_dir>. Absent checksums.sha = loud WARN, install continues:
#     post-DGN-1549 a git-tag-derived artifact legitimately ships none (its
#     bytes came out of `git archive <verified tag>` -- integrity is the
#     object store's), and pre-NM3 legacy packs never had one.
#
# WHY THIS IS A FUNCTION (DGN-1045 follow-up): the gate used to exist ONLY as
# an inline block on the legacy agent/module path. The kit/pack CONTRACT-CLASS
# path dispatches earlier and exits 0 on success, so it never reached the
# block -- measured 2026-08-25: a kind=pack fixture whose checksums.sha
# declared a deliberately wrong sha256 for its payload installed at rc 0 with
# no NM3 line anywhere, and packs/dev's own seal had been stale across 4
# commits with nothing looking at it. Neither compat-lint side hashes payload
# bytes, so the contract class -- the class every NEW and every THIRD-PARTY
# pack belongs to -- had NO integrity gate at all, while the deprecated class
# kept one. Hoisting it to a function called from BOTH paths is what makes
# packs/CONTRACT.md §5-1-5 ("계약팩 경로 5단계 NM3") true instead of aspirational.
_nm3_verify() {
  local pkg_dir="$1" payload_sub="${2:-.}"
  local sums_file="$pkg_dir/checksums.sha"
  local nm3_out=""

  # ---- lane split (DGN-1549): git work tree => tag-tree identity ----------
  local _nm3_top=""
  if _nm3_top="$(git -C "$pkg_dir" rev-parse --show-toplevel 2>/dev/null)" && [[ -n "$_nm3_top" ]]; then
    local _nm3_ref=""
    case "${EVIDENCE_SOURCE:-}" in
      tag:*) _nm3_ref="${EVIDENCE_SOURCE#tag:}" ;;
    esac
    if [[ -f "$sums_file" ]]; then
      _log "NM3: NOTE -- checksums.sha present at the git coordinate and IGNORED (DGN-1549: inside a work tree the seal is the tag's tree; a checksum file here is the exact artifact that went stale unseen)"
    fi
    if [[ -z "$_nm3_ref" ]]; then
      # Inside a work tree the source-tag gate has already forced a verdict:
      # reaching here without tag evidence means a DECLARED untagged-local
      # install or an unversioned legacy pack -- there is no seal to verify,
      # and the declaration/receipt carries that risk on the record.
      _log "NM3: SKIP -- git work-tree source with no tag evidence (untagged-local/unversioned): no seal exists to verify; the source= declaration in the receipt owns this risk (DGN-1549)"
      return 0
    fi
    _log "NM3: verifying payload against the sealed tag tree ($_nm3_ref) -- git-object identity, not a checksum file (DGN-1549)"
    local _nm3_diff=""
    _nm3_diff="$(git -C "$pkg_dir" diff --name-status "refs/tags/$_nm3_ref" -- . 2>&1)" \
      || _fail "NM3: git diff against refs/tags/$_nm3_ref failed in $_nm3_top -- cannot judge the seal, fail-closed"
    local _nm3_stray=""
    _nm3_stray="$(git -C "$pkg_dir" status --porcelain --ignored=matching -- "$payload_sub" 2>&1)" \
      || _fail "NM3: git status failed in $_nm3_top -- cannot judge the seal, fail-closed"
    # DGN-1698: when the install reads a sealed tag extraction
    # (PACK_TAG_TREE_TMP), git-IGNORED files at the coordinate cannot ride
    # -- nothing copies from the work tree -- so they are noted, not refused.
    # Untracked non-ignored extras stay a refusal (the checkout has moved).
    if [[ -n "${PACK_TAG_TREE_TMP:-}" ]]; then
      local _nm3_ign=""
      _nm3_ign="$(grep -c '^!!' <<<"$_nm3_stray" || true)"
      if [[ "${_nm3_ign:-0}" -gt 0 ]]; then
        _log "NM3: NOTE -- $_nm3_ign git-ignored path(s) under $payload_sub/ at the coordinate are NOT installed (install reads the sealed tag extraction; DGN-1698)"
      fi
      _nm3_stray="$(grep -E '^\?\?' <<<"$_nm3_stray" || true)"
    else
      _nm3_stray="$(grep -E '^(\?\?|!!)' <<<"$_nm3_stray" || true)"
    fi
    if [[ -n "$_nm3_diff" || -n "$_nm3_stray" ]]; then
      if [[ -n "$_nm3_diff" ]]; then
        _log "NM3: work tree differs from the sealed tag tree ($_nm3_ref) -- tracked drift:"
        while IFS= read -r _l; do _log "  NM3FAIL DRIFT: ${_l}"; done <<< "$_nm3_diff"
      fi
      if [[ -n "$_nm3_stray" ]]; then
        _log "NM3: unsealed extra files under $payload_sub/ (untracked/ignored -- these would install with no seal at all):"
        while IFS= read -r _l; do _log "  NM3FAIL UNSEALED: ${_l}"; done <<< "$_nm3_stray"
      fi
      _fail "NM3: the bytes at this coordinate are NOT the bytes $_nm3_ref sealed -- the work tree has moved (dirty edits / different checkout / unsealed extras). This is a stale-coordinate refusal, NOT payload corruption. Fix: check out the tag (or commit and tag the current state), or install through the tag-archive lane (update_apply). (DGN-1549)"
    fi
    _log "NM3: payload == $_nm3_ref tree (git-object identity) -- seal verified"
    return 0
  fi

  # ---- non-git lane: checksums.sha (artifact seal) ------------------------
  if [[ ! -f "$sums_file" ]]; then
    _log "NM3: WARN -- no checksums.sha in package ($sums_file) -- checksum verification SKIPPED (legitimate for a git-tag-derived artifact post-DGN-1549 and for pre-NM3 legacy packs; a non-git-published export SHOULD ship one, loud not silent)"
    return 0
  fi
  _log "NM3: verifying payload against checksums.sha"
  nm3_out="$("$PACK_PY3" - "$pkg_dir" "$sums_file" <<'PYEOF'
import hashlib, os, sys
pkg_dir, sums = sys.argv[1], sys.argv[2]
bad = []
n = 0
with open(sums) as f:
    for raw in f:
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # '<hex>  <relpath>' -- split on the first run of spaces (relpath may
        # contain single spaces, so split(None, 1) then re-strip is unsafe;
        # the publish format uses exactly two spaces as the separator).
        parts = line.split("  ", 1)
        if len(parts) != 2:
            bad.append("MALFORMED: %r" % line)
            continue
        want, rel = parts[0].strip(), parts[1].strip()
        p = os.path.join(pkg_dir, rel)
        if not os.path.isfile(p):
            bad.append("MISSING: %s" % rel)
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fp:
            for chunk in iter(lambda: fp.read(65536), b""):
                h.update(chunk)
        got = h.hexdigest()
        if got != want:
            bad.append("MISMATCH: %s (want %s got %s)" % (rel, want[:12], got[:12]))
        n += 1
if bad:
    for b in bad:
        print("NM3FAIL " + b)
    sys.exit(1)
print("NM3OK verified %d files" % n)
PYEOF
)" || {
    while IFS= read -r _l; do _log "  ${_l}"; done <<< "$nm3_out"
    _fail "NM3: payload checksum verification FAILED -- the payload does not match its checksums.sha seal: either the payload was corrupted/tampered in transit, or the seal itself was already stale when this version was cut (the DGN-1549 shape: dev pack 1.3.1/1.4.0 were sealed against a 2026-08-25 manifest nobody refreshed). A stale-sealed tag is permanently defective -- re-cut the version; do NOT regenerate the seal consumer-side. Install aborted (B4-5 gate)."
  }
  _log "  ${nm3_out}"
  _log "NM3: checksum verification passed"
}

# ---------- atomic conf upsert primitive (TK-13 U1, DGN-681 lock L214-217) ---
# _packs_upsert_atomic <conf> <pack_id> <version>
# Shared ATOMIC upsert for the .instance.conf DOGANY_PACKS line. Replaces the
# two previously non-atomic write_text sites (kit path + agent/module path);
# any future .instance.conf registry write (DOGANY_MODULES seeding, TK-13 U3)
# MUST use this same primitive ("동일 원자 함수로 통일", lock L217).
#   - flock on <conf>.lock serializes the WHOLE read-modify-write: rename
#     alone cannot stop lost updates -- two writers reading the same base
#     would let the last rename win. Lock acquisition waits up to
#     DOGANY_CONF_LOCK_TIMEOUT seconds (default 30; env override exists for
#     test hermeticity only, same doctrine as DOGANY_SHARED_HOME), then
#     FAILS LOUDLY with the conf untouched.
#   - tmp file in the SAME directory + fsync + os.replace: the swap is atomic
#     on a local filesystem, so a mid-write crash/kill leaves the ORIGINAL
#     conf intact (never a truncated/partial .instance.conf -- update.sh,
#     class gates and pack version compares all read this file).
#   - flock portability: local-disk semantics only, accepted per TK-13 RESPEC
#     §5 residual risk 4 (single-host estate; NFS out of scope).
#   - DOGANY_TEST_ATOMIC_UPSERT_DELAY (test-only): sleep between fsync and
#     rename to make the crash window / lock hold deterministic in tests.
# Failure => non-zero exit (set -e stops the install), original conf intact.
_packs_upsert_atomic() {
  local conf="$1" pid="$2" ver="$3"
  "$PACK_PY3" - "$conf" "$pid" "$ver" <<'PYEOF'
import fcntl, os, sys, tempfile, time, pathlib
conf = pathlib.Path(sys.argv[1])
pid, ver = sys.argv[2], sys.argv[3]
lock_path = str(conf) + ".lock"
timeout = float(os.environ.get("DOGANY_CONF_LOCK_TIMEOUT", "30"))
lock_fh = open(lock_path, "a")
deadline = time.monotonic() + timeout
while True:
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.monotonic() >= deadline:
            sys.stderr.write(
                "[packs-record] FATAL: could not acquire %s within %.0fs "
                "(concurrent writer stuck?) -- conf untouched\n" % (lock_path, timeout))
            sys.exit(1)
        time.sleep(0.05)
try:
    # Read-modify (byte-identical semantics with the pre-U1 upsert logic:
    # other packs' entries preserved, own id entry replaced, line appended
    # when absent -- DGN-227 B3/P7).
    lines = conf.read_text(encoding="utf-8").splitlines() if conf.exists() else []
    entry = f"{pid}@{ver}"
    found = False
    for i, ln in enumerate(lines):
        if ln.startswith("DOGANY_PACKS="):
            items = [x for x in ln.split("=", 1)[1].split(",") if x]
            items = [x for x in items if x.split("@", 1)[0] != pid]
            items.append(entry)
            lines[i] = "DOGANY_PACKS=" + ",".join(items)
            found = True
            break
    if not found:
        lines.append("DOGANY_PACKS=" + entry)
    data = "\n".join(lines) + "\n"
    # Atomic write: tmp in the SAME dir (rename atomicity condition) ->
    # fsync -> os.replace. Preserve the existing file mode (mkstemp gives
    # 0600; the pre-U1 write_text kept the original mode).
    mode = conf.stat().st_mode & 0o7777 if conf.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".instance.conf.tmp.", dir=str(conf.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        _delay = os.environ.get("DOGANY_TEST_ATOMIC_UPSERT_DELAY")
        if _delay:  # test-only crash-window / lock-hold injection
            time.sleep(float(_delay))
        os.replace(tmp, conf)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(str(conf.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    print(f"[packs-record] DOGANY_PACKS upserted: {entry}")
finally:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()
PYEOF
}

# _reqfw_record <root> <pack_id> <manifest_file>  (DGN-1031)
# Persists the pack's requires_framework range into a dedicated per-pack file
# (config/packs/<id>.requires_framework) so update.sh's framework-UPDATE-time
# compat gate (fw_reqframework_guard) can evaluate it without needing the
# pack's source manifest to still be reachable.
#
# Why not re-read the live manifest at update time instead of persisting a
# copy: a kit-class pack's source repo (e.g. dogany-lifekit) is a SIBLING
# checkout known to this script only via a transient --pack-dir/catalog path
# supplied on THIS install's command line -- that path is never persisted, so
# it cannot be reconstructed later. Worse, the DEFAULT framework update
# channel (self-update.sh "release") runs update.sh from a throwaway
# `git archive` extraction with no sibling checkouts at all, so even
# packs/catalog.json's package_dir entries (e.g. "../../dogany-lifekit")
# would resolve outside that temp tree to nothing. The manifest is durably
# UNREACHABLE at framework-update time -- so the value is captured HERE, at
# install time, while the manifest is in hand.
#
# Field absent in the manifest -> no file written (and any stale file from a
# prior install of this pack id is removed) -- "no constraint declared" is a
# real, valid state the update-time guard treats as SKIP, not a violation.
# Field present (even if malformed -- compat-lint's C1 should have already
# refused a malformed range at install time, but this script does not assume
# that gate ran) -> written VERBATIM. A malformed record must fail closed at
# guard time, never be silently treated as absent (DGN-1031 dispatch note).
_reqfw_record() {
  local root="$1" pid="$2" manifest="$3"
  local val dir file
  val="$("$PACK_PY3" -c "
import json, sys
d = json.load(open(sys.argv[1]))
v = d.get('requires_framework')
print(v if isinstance(v, str) else '')
" "$manifest" 2>/dev/null || echo "")"
  dir="$root/config/packs"
  file="$dir/$pid.requires_framework"
  mkdir -p "$dir"
  if [[ -n "$val" ]]; then
    printf '%s\n' "$val" > "$file"
    _log "  requires_framework recorded: $pid -> '$val' ($file)"
  else
    rm -f "$file"
    _log "  requires_framework: $pid manifest declares no constraint -- no record written"
  fi
}

# _post_mint_next_record <root> <pack_id> <manifest_file>  (DGN-1632)
# Persists the pack's optional top-level 'post_mint_next' string into
# config/packs/<id>.post_mint_next -- the durable on-instance proxy the
# DGN-1487 slot resolver already reads (onboarding-check.py
# resolve_post_mint_next), same pattern as _reqfw_record's
# <id>.requires_framework above. The framework treats the value as an
# OPAQUE blob it never parses; only the pack's own wiring gives it meaning.
#
# Field absent/null/whitespace-only -> unset: any stale record from a prior
# install of this pack id is removed (an instance-pinned empty value must
# never permanently shadow a fresh manifest that dropped the field). Field
# present as a nonblank string -> written VERBATIM plus one terminal
# newline. A non-string non-null value is an installation error (loud FAIL,
# no partial write) -- a malformed declaration must fail closed at install
# time, not be silently coerced or ignored.
#
# Write discipline: same-directory temp file + os.replace (atomic rename,
# no partial file ever visible), Python JSON parsing only -- never eval the
# value or interpolate it as shell code, never touch config/agent.conf or
# any other pack's record. A write failure is propagated (nonzero exit):
# the caller must not report a successful install over a lost record.
_post_mint_next_record() {
  local root="$1" pid="$2" manifest="$3"
  local dir="$root/config/packs"
  mkdir -p "$dir"
  "$PACK_PY3" -c "
import json, os, sys

root, pid, manifest_path = sys.argv[1], sys.argv[2], sys.argv[3]
dir_path = os.path.join(root, 'config', 'packs')
file_path = os.path.join(dir_path, pid + '.post_mint_next')

with open(manifest_path, encoding='utf-8') as f:
    manifest = json.load(f)
val = manifest.get('post_mint_next')

if val is None:
    if os.path.exists(file_path):
        os.remove(file_path)
    print('  post_mint_next: %s manifest declares no continuation -- no record written' % pid)
    sys.exit(0)

if not isinstance(val, str):
    print('post_mint_next: %s manifest field is not a string (got %s)' % (pid, type(val).__name__), file=sys.stderr)
    sys.exit(1)

if not val.strip():
    if os.path.exists(file_path):
        os.remove(file_path)
    print('  post_mint_next: %s manifest declares a blank continuation -- treated as unset, no record written' % pid)
    sys.exit(0)

tmp_path = file_path + '.tmp'
with open(tmp_path, 'w', encoding='utf-8') as f:
    f.write(val + '\n')
os.replace(tmp_path, file_path)
print('  post_mint_next recorded: %s -> %r (%s)' % (pid, val, file_path))
" "$root" "$pid" "$manifest" 2>&1 | while IFS= read -r _line; do _log "$_line"; done
  return "${PIPESTATUS[0]}"
}

# _tone_candidates_record <root> <pack_id> <manifest_file>  (DGN-1632 W3)
# Persists the pack's optional top-level 'tone_candidates' object into
# config/packs/<id>.tone_candidates.json -- the on-instance proxy the
# onboarding SessionStart hook reads for the Q4 tone question
# (onboarding-check.py resolve_tone_candidates). Same record discipline as
# _post_mint_next_record above: absent/null removes any stale record; a
# malformed declaration is an installation error (loud FAIL, no partial
# write); temp file + os.replace; never touches config/agent.conf or another
# pack's record. Shape (packs/CONTRACT.md section 2):
#   {"<lang>": [{"label": "...", "sample": "..."}, ... 3-4 items], ...}
# lang = two lowercase letters; label/sample = nonblank one-line strings; a
# "ko" label ends with the '<adjective> style' word (U+C2A4 U+D0C0 U+C77C).
_tone_candidates_record() {
  local root="$1" pid="$2" manifest="$3"
  local dir="$root/config/packs"
  mkdir -p "$dir"
  "$PACK_PY3" -c "
import json, os, re, sys

root, pid, manifest_path = sys.argv[1], sys.argv[2], sys.argv[3]
dir_path = os.path.join(root, 'config', 'packs')
file_path = os.path.join(dir_path, pid + '.tone_candidates.json')
KO_STYLE = '\uc2a4\ud0c0\uc77c'

with open(manifest_path, encoding='utf-8') as f:
    manifest = json.load(f)
val = manifest.get('tone_candidates')

if val is None:
    if os.path.exists(file_path):
        os.remove(file_path)
    print('  tone_candidates: %s manifest declares none -- no record written' % pid)
    sys.exit(0)

def bad(why):
    print('tone_candidates: %s %s' % (pid, why), file=sys.stderr)
    sys.exit(1)

if not isinstance(val, dict) or not val:
    bad('must be a non-empty object keyed by language')
for lang, items in val.items():
    if not re.fullmatch(r'[a-z]{2}', lang):
        bad('language key %r is not two lowercase letters' % lang)
    if not isinstance(items, list) or not 3 <= len(items) <= 4:
        bad('%s must be a list of 3-4 candidates' % lang)
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {'label', 'sample'}:
            bad('%s[%d] must have exactly the keys label, sample' % (lang, i))
        for key in ('label', 'sample'):
            v = item[key]
            if not isinstance(v, str) or not v.strip() or '\n' in v:
                bad('%s[%d].%s must be a nonblank one-line string' % (lang, i, key))
        if lang == 'ko' and not item['label'].strip().endswith(' ' + KO_STYLE):
            bad('ko[%d].label must use the <adjective> style phrasing' % i)

tmp_path = file_path + '.tmp'
with open(tmp_path, 'w', encoding='utf-8') as f:
    json.dump(val, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write('\n')
os.replace(tmp_path, file_path)
print('  tone_candidates recorded: %s -> %s (%s)' % (pid, ', '.join(sorted(val)), file_path))
" "$root" "$pid" "$manifest" 2>&1 | while IFS= read -r _line; do _log "$_line"; done
  return "${PIPESTATUS[0]}"
}

# ---------- resolve pack from the catalog (or --pack-dir override) -----------
if [[ -n "$PACK_DIR_OVERRIDE" ]]; then
  # Kit-class (or any pack) installed directly from a directory.
  PACKAGE_DIR="$(cd "$PACK_DIR_OVERRIDE" && pwd)"
  PACK_JSON=""  # not catalog-derived
else
  # DGN-1079: catalog row read through the shared coordinate resolver -- the
  # publisher reads the SAME function, so publisher and installer cannot
  # disagree about where a pack lives.
  PACK_JSON="$(pack_catalog_row "$CATALOG" "$PACK_ID")"

  [[ "$PACK_JSON" != "null" && -n "$PACK_JSON" ]] || _fail "pack not found in catalog: $PACK_ID"
fi

# Read fields from pack JSON (catalog-derived entries only).
_pack_field() {
  [[ -n "$PACK_JSON" ]] || { echo ""; return 0; }
  pack_row_field "$PACK_JSON" "$1"
}

# For catalog-derived packs PACKAGE_DIR comes from pack_json; for --pack-dir it
# was already set above.
if [[ -z "$PACK_DIR_OVERRIDE" ]]; then
  PACKAGE_DIR="$(_pack_field package_dir)"
fi
DOMAIN_FIELD="$(_pack_field "id")"
# DGN-227 B3/P6: catalog entry pack_version (semver). Legacy entries without
# the field install as 'unversioned' (loud in the ledger header).
PACK_VERSION="$(_pack_field pack_version)"
PACK_VERSION="${PACK_VERSION:-unversioned}"

if [[ -z "$PACK_DIR_OVERRIDE" ]]; then
  [[ -n "$PACKAGE_DIR" ]] || _fail "pack '$PACK_ID' missing 'package_dir' field in catalog"
  # Resolve package_dir relative to the CATALOG FILE location (absolute allowed).
  # DGN-1079: the rule now lives in scripts/pack/lib/pack_coords.sh so
  # pack_publish.sh resolves the identical coordinate instead of inventing one.
  PACKAGE_DIR="$(pack_coord_resolve "$CATALOG" "$PACKAGE_DIR")"
  # DGN-1818 public-install lane: no sibling checkout here -> the row's
  # source_url, cloned once under the shared home (pack_coords.sh). The
  # clone is a git work tree at a pack tag, so D1 / source-tag / NM3 below
  # judge it exactly like a sibling checkout. A sibling that exists wins.
  if [[ ! -f "$PACKAGE_DIR/pack-manifest.json" ]]; then
    _src_url="$(_pack_field source_url)"
    if [[ -n "$_src_url" ]]; then
      # --dry-run writes nothing to the shared home: it clones into a private
      # tmp and judges that copy (same bytes, same gates).
      _src_home="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
      if [[ "$DRY" -eq 1 ]]; then
        _src_home="$(mktemp -d "${TMPDIR:-/tmp}/pack-src-dry.XXXXXX")" || _fail "pack source: mktemp failed"
      fi
      PACKAGE_DIR="$(DOGANY_SHARED_HOME="$_src_home" pack_source_fetch "$PACK_ID" "$_src_url" "$SOURCE_TAG")" \
        || _fail "pack source: cannot fetch $PACK_ID from $_src_url (see the reason above)"
      unset _src_home
      _log "pack source: $PACK_ID fetched from $_src_url -> $PACKAGE_DIR ($(git -C "$PACKAGE_DIR" describe --tags --exact-match 2>/dev/null || echo '?'))"
    fi
    unset _src_url
  fi
fi

# ---------- pack manifest (declaration-driven install) ------------------------
MANIFEST="$PACKAGE_DIR/pack-manifest.json"
[[ -f "$MANIFEST" ]] || _fail "pack-manifest.json not found: $MANIFEST (every pack must declare its categories -- no legacy fallback)"

_mf_field() { # _mf_field <key> -- string field ('' when absent)
  "$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print(v if isinstance(v,str) else '')" "$MANIFEST" "$1"
}

# Read kind and kit fields for early dispatch.
PACK_KIND="$(_mf_field kind)"
KIT_PROVIDES="$(_mf_field provides_kit)"
KIT_PAYLOAD_ROOT="$(_mf_field payload_root)"
KIT_PAYLOAD_ROOT="${KIT_PAYLOAD_ROOT:-payload}"

# Supplement pack_version from the manifest when the catalog did not provide
# one: --pack-dir installs have no catalog row at all, and kit-class catalog
# rows (DGN-803 LS-5 e2) deliberately omit pack_version -- the external pack
# repo's manifest is authoritative, a catalog copy would drift.
if [[ -z "$PACK_VERSION" || "$PACK_VERSION" == "unversioned" ]]; then
  _MF_PACK_VERSION="$(_mf_field pack_version)"
  [[ -n "$_MF_PACK_VERSION" ]] && PACK_VERSION="$_MF_PACK_VERSION"
  PACK_VERSION="${PACK_VERSION:-unversioned}"
fi

# ---------------------------------------------------------------------------
# --source-tag: the ledger records the INSTALLED tag's version (dsp t2pin)
# ---------------------------------------------------------------------------
# Measured defect: DOGANY_PACKS recorded $PACK_VERSION (manifest/catalog)
# verbatim, so installing from a pre-release tag whose manifest still carried
# the base version (pack/pack_health/v0.11.0-dev.1 with pack_version=0.11.0,
# the 2026-09-14 dogfooding cut) DROPPED the -dev.1 suffix. Two harms: the
# ledger pinned a version whose tag does not exist (pin_untagged), and the
# later real v0.11.0 cut would read as "already installed" (upgrade never
# detected). RECORD_VERSION is what the ledger writes; with --source-tag it
# is the tag's version segment, so the ledger alone distinguishes a dev
# install from a stable one. Without the flag, RECORD_VERSION == PACK_VERSION
# and every existing lane stays byte-identical.
RECORD_VERSION="$PACK_VERSION"
if [[ -n "$SOURCE_TAG" ]]; then
  _st_prefix="pack/$PACK_ID/v"
  case "$SOURCE_TAG" in
    "$_st_prefix"?*) : ;;
    *) _fail "--source-tag '$SOURCE_TAG' does not follow the pack tag grammar for this pack (expected ${_st_prefix}<version> -- dec-120 #1); wrong id or malformed ref, fail-closed" ;;
  esac
  SOURCE_TAG_VERSION="${SOURCE_TAG#"$_st_prefix"}"
  _st_semver='^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$'
  [[ "$SOURCE_TAG_VERSION" =~ $_st_semver ]] \
    || _fail "--source-tag version segment '$SOURCE_TAG_VERSION' is not semver (X.Y.Z[-pre][+build]) -- refusing to write an unparseable ledger entry, fail-closed"
  # 3-point coherence, pre-release form: the tag may only ADD a pre-release/
  # build suffix to the manifest's declared version (the dogfooding shape).
  # Any other divergence is the manifest==tag 3-point violation -- it must
  # fail closed HERE, never land in a live ledger.
  _st_base="${SOURCE_TAG_VERSION%%[-+]*}"
  if [[ "$SOURCE_TAG_VERSION" != "$PACK_VERSION" && "$_st_base" != "$PACK_VERSION" ]]; then
    _fail "--source-tag version '$SOURCE_TAG_VERSION' vs resolved pack_version '$PACK_VERSION': neither equal nor a pre-release of it -- manifest==tag 3-point violation, fail-closed"
  fi
  # Tag existence: judgeable only when the pack source IS a git work tree
  # (same narrowing as the D1 gate). update_apply's tar extraction has no
  # .git -- its own refs/tags verify already ran repo-side before extraction.
  if _st_top="$(git -C "$PACKAGE_DIR" rev-parse --show-toplevel 2>/dev/null)" && [[ -n "$_st_top" ]]; then
    git -C "$PACKAGE_DIR" rev-parse -q --verify "refs/tags/$SOURCE_TAG" >/dev/null 2>&1 \
      || _fail "--source-tag '$SOURCE_TAG' does not exist in the pack source work tree ($_st_top) -- the ledger must never pin a tag that is not there (pin_untagged), fail-closed"
    _log "source-tag: $SOURCE_TAG verified present in $_st_top"
  else
    _log "source-tag: pack source is not a git work tree -- tag existence was judged by the caller (tag-archive extraction lane)"
  fi
  RECORD_VERSION="$SOURCE_TAG_VERSION"
  if [[ "$RECORD_VERSION" != "$PACK_VERSION" ]]; then
    _log "ledger version: $RECORD_VERSION (source tag) != manifest pack_version $PACK_VERSION -- pre-release install; the ledger records the tag's version"
  fi
fi

# DGN-1423: refuse a whole-pack downgrade before any payload dispatch.
# Reuse update_apply's SemVer precedence; a ledger is evidence, not permission
# to ignore the physical pin checks later in the contract lane.
_pi_prev=""
if [[ -f "$ROOT/.instance.conf" ]]; then
  _pi_prev="$(sed -n 's/^DOGANY_PACKS=//p' "$ROOT/.instance.conf" 2>/dev/null | head -1 | tr ',' '\n' | awk -F@ -v id="$PACK_ID" '$1 == id {print substr($0, length(id)+2)}')" \
    || _fail "pack admission: cannot read installed version evidence"
fi
# 'unversioned' is the installer's own legacy no-claim marker (D1 / source-tag
# gate precedent): it has no SemVer precedence, so it never reaches compare.
# No claim on either side of a legacy re-install -> SKIP loudly. Replacing a
# real installed version with no claim cannot be proven not-a-downgrade ->
# refuse. A first claim over a no-claim ledger entry is admitted.
if [[ -n "$_pi_prev" && ( "$_pi_prev" == "unversioned" || "$RECORD_VERSION" == "unversioned" ) ]]; then
  [[ "$RECORD_VERSION" == "unversioned" && "$_pi_prev" != "unversioned" ]] \
    && _fail "pack admission: downgrade not judgeable ($PACK_ID installed=$_pi_prev candidate=unversioned) -- an unversioned candidate would erase the installed version claim; refused"
  _log "pack admission: SKIP downgrade check -- no version claim to order ($PACK_ID installed=$_pi_prev candidate=$RECORD_VERSION; 'unversioned' legacy marker, D1 precedent)"
elif [[ -n "$_pi_prev" ]]; then
  _pi_cmp="$("$PACK_PY3" "$SCRIPT_DIR/lib/semver_range.py" compare "$RECORD_VERSION" "$_pi_prev")" \
    || _fail "pack admission: version comparison failed ($PACK_ID installed=$_pi_prev candidate=$RECORD_VERSION)"
  [[ "$_pi_cmp" != "-1" ]] \
    || _fail "pack admission: downgrade refused ($PACK_ID installed=$_pi_prev candidate=$RECORD_VERSION); automatic rollback is not supported"
fi

# ---------------------------------------------------------------------------
# D1 commit gate -- PRE-EVERYTHING
# ---------------------------------------------------------------------------
# "The pack_version this install is about to write to a live instance must
# have a commit." Rule, measurement, narrowing and the no-escape-hatch
# argument all live in lib/pack_commit_gate.sh; do not restate them here.
#
# POSITION: this is the FIRST gate in the file, ahead of requires_kit /
# compat-lint / NM3 / preflight, and ahead of every dispatch. That is
# deliberate -- it is the cheapest gate (one git log) and the only one whose
# failure means "nothing here is reproducible", so it should be what a
# doomed install pays for first. It sits after PACK_VERSION resolution
# because the value it judges is the resolved one.
#
# DRY-RUN: this gate FIRES in --dry-run too. A dry run answers "would this
# install be admitted"; answering "yes" for a version the real run will
# refuse would make the dry run a liar. It writes nothing either way.
# `|| true` + empty-check: a crashed predicate (no python3, killed git) must
# produce a BLOCK with a printed reason, never a bare set -e death.
_d1_out="$(pack_d1_verdict "$MANIFEST" "$PACK_VERSION" || true)"
[[ -n "$_d1_out" ]] || _d1_out="BLOCK|the D1 predicate produced no verdict (python3 or git unavailable?) -- cannot judge, fail-closed"
_d1_verdict="${_d1_out%%|*}"
_d1_detail="${_d1_out#*|}"
case "$_d1_verdict" in
  PASS) _log "D1 commit gate: PASS -- $_d1_detail" ;;
  SKIP) _log "D1 commit gate: SKIP -- $_d1_detail" ;;
  *)    _log "D1 commit gate: BLOCK -- $_d1_detail"
        _log "  what this means: the bytes this install would put on the live"
        _log "  instance are named by a version that exists only in a working"
        _log "  tree. Reverting that tree destroys the only copy -- there is no"
        _log "  SHA that reproduces what would be installed (measured: E11,"
        _log "  kit_life 2.6.0, 69 minutes live with no reproduction path)."
        _log "  fix: commit the pack source, then re-run this install."
        _log "    git -C <pack source> add pack-manifest.json && git -C <pack source> commit"
        _log "  there is no override flag, by design (release.sh precedent)."
        _fail "D1: pack_version=$PACK_VERSION has no commit -- install refused (D1 gate)"
        ;;
esac
unset _d1_out _d1_verdict _d1_detail

# ---------------------------------------------------------------------------
# Source-tag gate (DGN-1489) -- the ledger only pins tag-backed versions
# ---------------------------------------------------------------------------
# Measured defect: kit_life@2.20.1 landed in a live instance ledger while
# `git tag --list pack/kit_life/v2.20.1` returned 0 hits -- --source-tag is
# optional, so a flag-less install recorded the working-tree manifest version
# with nobody checking a tag existed (the pin_untagged shape, handoff
# 01M2J3TSR61ZX4SXNGZSAKJ2X0 to the leader). DGN-1471 wired --source-tag into the
# mint/update lanes; this gate is the CONVERGENCE ENDPOINT: fixing call sites
# one by one cannot survive the next new call site, so the machine judges
# every lane here, after the caller-provided declarations are known.
#
# Verdicts (scope = pack source inside a git work tree, the SAME narrowing as
# D1 above -- the test estate installs non-git $TMPDIR fixtures and blocking
# those deletes the harness, not a defect; a pack copied out of git to dodge
# the gate is not covered -- stated, not hidden):
#   --source-tag given          -> already validated fail-closed above
#                                  (grammar, semver, 3-point, tag existence)
#   --untagged-local <reason>   -> ADMITTED, loudly; the receipt records
#                                  source=untagged-local:<reason>
#   neither, tag pack/<id>/v<resolved version> EXISTS
#                               -> ADMITTED (auto-verified tag-backed install;
#                                  covers install.sh's fresh-mint catalog lane
#                                  which cannot know pre-release refs)
#   neither, tag absent         -> REFUSED. The fix is to tag/publish the
#                                  version, name the real tag with
#                                  --source-tag, or DECLARE the untagged
#                                  install. No silent lane remains.
#   pack_version=unversioned    -> SKIP loudly (no value to judge -- the D1
#                                  'unversioned' precedent, legacy no-claim
#                                  marker)
#
# POSITION: after D1 on purpose. D1 is the cheaper predicate and its PASS
# line is part of the install ledger contract (test-pack-d1-commit-gate.sh
# D2 asserts it prints before any later gate refuses).
#
# EVIDENCE_SOURCE is what evidence_begin writes into the receipt's source=
# line (empty = no line, byte-identical receipts for out-of-scope lanes).
EVIDENCE_SOURCE=""
if [[ -n "$SOURCE_TAG" ]]; then
  EVIDENCE_SOURCE="tag:$SOURCE_TAG"
elif _tg_top="$(git -C "$PACKAGE_DIR" rev-parse --show-toplevel 2>/dev/null)" && [[ -n "$_tg_top" ]]; then
  if [[ -n "$UNTAGGED_REASON" ]]; then
    EVIDENCE_SOURCE="untagged-local:$UNTAGGED_REASON"
    _log "source-tag gate: UNTAGGED-LOCAL install declared -- reason: $UNTAGGED_REASON"
    _log "  ledger will record $PACK_ID@$RECORD_VERSION with NO tag backing; the"
    _log "  receipt keeps the declaration (source=untagged-local) for audit"
  elif [[ "$PACK_VERSION" == "unversioned" ]]; then
    _log "source-tag gate: SKIP -- pack declares no version (unversioned) -- no tag to require (D1 precedent)"
  else
    _tg_tag="$(pack_d1_tag_ref "$PACK_ID" "$PACK_VERSION")"
    if git -C "$PACKAGE_DIR" rev-parse -q --verify "refs/tags/$_tg_tag" >/dev/null 2>&1; then
      EVIDENCE_SOURCE="tag:$_tg_tag"
      _log "source-tag gate: PASS -- no --source-tag given, but $_tg_tag exists in $_tg_top (auto-verified tag-backed install)"
    else
      _log "source-tag gate: BLOCK -- pack_version=$PACK_VERSION has no tag $_tg_tag in $_tg_top"
      _log "  what this means: this install would pin a version in the live ledger"
      _log "  whose tag does not exist (pin_untagged) -- the exact shape that put"
      _log "  kit_life@2.20.1 on a live instance with zero tags (DGN-1489)."
      _log "  fix, pick one:"
      _log "    - tag/publish the version in the pack repo (the owner seal lane), or"
      _log "    - pass --source-tag <the real ref> (pre-release installs), or"
      _log "    - declare --untagged-local <reason> for a deliberate untagged install"
      _fail "source-tag gate: pack_version=$PACK_VERSION is not tag-backed and no declaration was made -- install refused (DGN-1489)"
    fi
    unset _tg_tag
  fi
  unset _tg_top
else
  # Out of scope (non-git pack source: $TMPDIR fixtures, tar extraction).
  # The tar lane's tag existence was judged repo-side before extraction and
  # arrives here WITH --source-tag, so it never reaches this branch.
  if [[ -n "$UNTAGGED_REASON" ]]; then
    EVIDENCE_SOURCE="untagged-local:$UNTAGGED_REASON"
  fi
  _log "source-tag gate: SKIP -- pack source is not inside a git work tree (test fixture / unpacked payload lane)"
fi

# ---------------------------------------------------------------------------
# Sealed tag tree (DGN-1698 fix A) -- install reads the TAG, not the checkout
# ---------------------------------------------------------------------------
# Measured (rehearsal 5, 2026-09-25 11:21): an owner mint's kit auto-resolve
# died at compat-lint C4b on payload/database/__pycache__/*.pyc in the real
# dogany-lifekit checkout -- git-IGNORED files a developer worker's run of
# kit_life.sh had just written there. `git status --porcelain` was clean.
# Every install-time content check (C4b hygiene, C5 personal-data scan, the
# skill-collision gate, the kind/ns surface checks) and every payload copy
# walked the WORK TREE, so machine-local residue that was never part of the
# seal could both block an owner install and ride into the instance.
#
# Rule: once the source-tag gate has named a verified tag for a git work
# tree source (EVIDENCE_SOURCE=tag:<ref>), the package is materialized from
# `git archive refs/tags/<ref>` -- the same object-store identity NM3 checks
# -- and PACKAGE_DIR / MANIFEST are re-pointed at that extraction. Every
# check and copy below therefore sees exactly the sealed tree: ignored or
# untracked files in the checkout are invisible to it and cannot be copied.
# PACKAGE_SRC_DIR keeps the work tree coordinate for the git-side readers
# (NM3's tag-identity check, the content-drift baseline repo lookup).
# NM3 still refuses TRACKED drift and untracked (non-ignored) extras at the
# coordinate -- a moved checkout remains a stale-coordinate refusal; only
# git-ignored residue stops blocking, because it can no longer install.
# Out of scope (unchanged): non-git sources (tar lane, fixtures) and
# untagged-local / unversioned installs, which have no seal to read.
# Boundary: git archive honors export-ignore attributes; no estate pack repo
# carries .gitattributes (measured 2026-09-18, see NM3), so archive == tree.
PACKAGE_SRC_DIR="$PACKAGE_DIR"
PACK_TAG_TREE_TMP=""
case "$EVIDENCE_SOURCE" in
  tag:*)
    if _tt_top="$(git -C "$PACKAGE_DIR" rev-parse --show-toplevel 2>/dev/null)" && [[ -n "$_tt_top" ]]; then
      _tt_ref="${EVIDENCE_SOURCE#tag:}"
      _tt_sub="$(git -C "$PACKAGE_DIR" rev-parse --show-prefix 2>/dev/null)" \
        || _fail "sealed tag tree: cannot locate $PACKAGE_DIR inside $_tt_top -- fail-closed (DGN-1698)"
      _tt_sub="${_tt_sub%/}"
      PACK_TAG_TREE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dogany-pack-tag.XXXXXX")" \
        || _fail "sealed tag tree: cannot create an extraction dir for $_tt_ref -- fail-closed (DGN-1698)"
      trap '[[ -n "$PACK_TAG_TREE_TMP" ]] && rm -rf "$PACK_TAG_TREE_TMP"' EXIT
      if ! git -C "$_tt_top" archive --format=tar "refs/tags/$_tt_ref" ${_tt_sub:+-- "$_tt_sub"} \
           | tar -xf - -C "$PACK_TAG_TREE_TMP"; then
        _fail "sealed tag tree: git archive refs/tags/$_tt_ref failed in $_tt_top -- cannot read the seal, fail-closed (DGN-1698)"
      fi
      _tt_dir="$PACK_TAG_TREE_TMP${_tt_sub:+/$_tt_sub}"
      [[ -f "$_tt_dir/pack-manifest.json" ]] \
        || _fail "sealed tag tree: $_tt_ref carries no pack-manifest.json at '${_tt_sub:-.}' -- fail-closed (DGN-1698)"
      PACKAGE_DIR="$_tt_dir"
      MANIFEST="$PACKAGE_DIR/pack-manifest.json"
      _log "sealed tag tree: installing from $_tt_ref (git archive of $_tt_top${_tt_sub:+ :$_tt_sub}) -- checks and copies read the tag, not the work tree (DGN-1698)"
      unset _tt_ref _tt_sub _tt_dir
    fi
    unset _tt_top
    ;;
esac

# ---------------------------------------------------------------------------
# Python caches are never payload (DGN-1788)
# ---------------------------------------------------------------------------
# Measured (DGN-1787 e2e, 2026-09-28 20:10): a gitignored
# payload/database/__pycache__/*.pyc in the dogany-lifekit checkout (written
# by any python import from the checkout) failed install-side compat-lint
# C4b and aborted a real mint, on a lane the sealed tag tree above does not
# cover (untagged-local / non-git source). __pycache__/ dirs and *.pyc/*.pyo
# files are machine-local artifacts: the installer never ships them. When the
# package tree carries any, it is materialized once into a cache-free copy
# and PACKAGE_DIR / MANIFEST are re-pointed at it, so every check and every
# payload copy below sees no cache (one strip, not one filter per cp loop).
# .git is not copied (git-side readers use PACKAGE_SRC_DIR). Publish side is
# unchanged: compat-lint C4b (no --install-side) still FAILs on a cache.
PACK_PYCACHE_TMP=""
_pc_list="$(find "$PACKAGE_DIR" -name .git -prune -o \
  \( -type d -name __pycache__ -print -prune \) -o \
  \( -type f \( -name '*.pyc' -o -name '*.pyo' \) -print \) 2>/dev/null)"
if [[ -n "$_pc_list" ]]; then
  PACK_PYCACHE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dogany-pack-nocache.XXXXXX")" \
    || _fail "python cache strip: cannot create a staging dir -- fail-closed (DGN-1788)"
  trap '[[ -n "$PACK_TAG_TREE_TMP" ]] && rm -rf "$PACK_TAG_TREE_TMP"; [[ -n "$PACK_PYCACHE_TMP" ]] && rm -rf "$PACK_PYCACHE_TMP"' EXIT
  _pc_dst="$PACK_PYCACHE_TMP/pkg"
  mkdir "$_pc_dst" \
    || _fail "python cache strip: cannot create $_pc_dst -- fail-closed (DGN-1788)"
  for _pc_e in "$PACKAGE_DIR"/* "$PACKAGE_DIR"/.[!.]* "$PACKAGE_DIR"/..?*; do
    [[ -e "$_pc_e" || -L "$_pc_e" ]] || continue
    [[ "$(basename "$_pc_e")" == ".git" ]] && continue
    cp -Rp "$_pc_e" "$_pc_dst/" \
      || _fail "python cache strip: copy of $_pc_e failed -- fail-closed (DGN-1788)"
  done
  find "$_pc_dst" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null
  find "$_pc_dst" -type f \( -name '*.pyc' -o -name '*.pyo' \) -exec rm -f {} + 2>/dev/null
  if [[ -n "$(find "$_pc_dst" \( -type d -name __pycache__ \) -o \( -type f \( -name '*.pyc' -o -name '*.pyo' \) \) 2>/dev/null)" ]]; then
    _fail "python cache strip: caches survived the strip in $_pc_dst -- fail-closed (DGN-1788)"
  fi
  while IFS= read -r _pc_l; do
    [[ -n "$_pc_l" ]] && _log "  WARN: python cache not installed (never payload, DGN-1788): ${_pc_l#"$PACKAGE_DIR/"}"
  done <<< "$_pc_list"
  PACKAGE_DIR="$_pc_dst"
  MANIFEST="$PACKAGE_DIR/pack-manifest.json"
  _log "python cache strip: installing from a cache-free copy of the package (DGN-1788)"
  unset _pc_dst _pc_e _pc_l
fi
unset _pc_list

# Resolve a symlink chain to its physical target (bash 3.2 safe -- no
# realpath/readlink -f guaranteed on macOS' bundled bash; same technique as
# scripts/dogany resolve_link). Prints the resolved path; rc 1 if a
# directory component along the chain cannot be entered (dangling link).
# Defined here (moved up from the kit-install section, DGN-1461) because the
# requires_kit scope gate below resolves crew DB links at gate time.
_kit_resolve_link() {
  local src="$1" dir base
  while [[ -L "$src" ]]; do
    dir="$(cd -P "$(dirname "$src")" 2>/dev/null && pwd)" || return 1
    base="$(readlink "$src")"
    case "$base" in
      /*) src="$base" ;;
      *)  src="$dir/$base" ;;
    esac
  done
  dir="$(cd -P "$(dirname "$src")" 2>/dev/null && pwd)" || return 1
  printf '%s/%s' "$dir" "$(basename "$src")"
}

# ---------------------------------------------------------------------------
# requires_kit satisfaction gate (DGN-681 S4b-1 §2-c) -- PRE-PAYLOAD-COPY
# ---------------------------------------------------------------------------
# Install-time enforcement of the pack's declared kit dependency. Runs BEFORE
# the install dispatch (so before ANY payload copy on either the kit-class or
# the agent/module path).
#
# INDEPENDENT DUPLICATION of the compat-lint C-KITDEP form check (DGN-1002
# lockstep precedent, same as the KIT_NAME block below): the compat-lint gate
# is structurally fail-open (WARN+skip when compat-lint.sh is absent), so the
# installer validates on its own -- this inline check ALWAYS runs. Form
# verdicts must stay verdict-identical with compat-lint C-KITDEP; if either
# side changes, change the other.
#
# Verdict table (DGN-681-S4b-RATIFIED §3-B -- ALL fail-closed, NO warn lane):
#   requires_kit absent                          -> SKIP (no kit dependency)
#   form damage (non-object / keys != {kit,range} / kit grammar violation /
#     reserved name / self-dep / range grammar violation / kind=kit declares)
#                                                -> BLOCK
#   .instance.conf absent / DOGANY_PACKS line absent
#                                                -> BLOCK
#   no <kit>@ entry in DOGANY_PACKS              -> BLOCK
#   entry version unparseable (incl. 'unversioned')
#                                                -> BLOCK (unverifiable =
#                                                   no-go, DGN-1004)
#   installed version outside range              -> BLOCK
#   satisfied                                    -> PASS + kit@version log
# Legacy pack (no contract_version AND kind != kit): gate not applicable --
# mirrors the compat-lint legacy-grace boundary so form verdicts stay
# verdict-identical (legacy packs never reach C-KITDEP either). This EXTENDS
# the existing grace line (absence observed = grace), it creates no new one.
# Wrapped in a function (not a one-shot inline capture) so the DGN-1406/1409
# auto-resolve seam below can re-derive the verdict AFTER installing a
# missing required kit, against the exact same predicate -- no second
# satisfaction-check implementation.
_rk_compute_verdict() {
"$PACK_PY3" - "$MANIFEST" "$ROOT/.instance.conf" "$PACK_TOKEN_CORE" "${PACK_RESERVED_TOKENS[*]}" <<'PYEOF'
import json, re, sys, pathlib

def out(v):
    print(v)
    sys.exit(0)

# DGN-1406/1409: a |KIT=<kit>|RANGE=<range> trailer on a BLOCK verdict is the
# ONLY signal the bash-side auto-resolve seam acts on. It is emitted SOLELY
# by the satisfaction-stage out_dep() calls below (kit/range already
# form-validated by that point) -- every form-damage BLOCK above stays a
# bare "BLOCK:<msg>" and is therefore untouched by that seam, on purpose:
# auto-resolving a MALFORMED declaration is not this change's job.
def out_dep(msg):
    out("BLOCK:%s|KIT=%s|RANGE=%s" % (msg, kit, rng))

# DGN-1018 RR2: regex core + reserved list are shared data, passed as argv
# since a heredoc body cannot see bash variables -- see
# lib/pack_token_grammar.sh. Anchored with \Z here, not a bash-style dollar
# anchor: a python dollar anchor also matches BEFORE a trailing newline,
# which the bash [[ =~ ]] twin rejects -- a dollar anchor here would accept
# a trailing newline and diverge, same defect class as the
# service_namespace NAME_RE below, DGN-1018 5-B.
_TOKEN_RE = re.compile('^' + sys.argv[3] + r'\Z')
_RESERVED = sys.argv[4].split()

try:
    d = json.load(open(sys.argv[1]))
    if not isinstance(d, dict):
        raise ValueError("manifest top-level JSON is not an object")
except Exception as e:
    out("BLOCK:pack-manifest.json unreadable (%s)" % e)

contract = d.get("contract_version")
kind = d.get("kind") or ""

# Legacy-grace mirror (compat-lint boundary, DGN-803 FIX-1 / DGN-1004):
# absence of contract_version genuinely observed + kind != kit = legacy pack;
# C-KITDEP is unreachable for it, so this inline twin is not applicable.
if (contract is None or contract == "") and kind != "kit":
    out("SKIP:legacy pack (no contract_version) -- requires_kit gate not applicable")

if "requires_kit" not in d:
    out("SKIP:requires_kit absent -- no kit dependency declared")

rk = d.get("requires_kit")

# --- form validation (verdict-identical with compat-lint C-KITDEP) ---
if kind == "kit":
    out("BLOCK:kind=kit must not declare requires_kit -- kit->kit dependency "
        "install semantics are undefined (undefined = refuse, DGN-1004)")
if not isinstance(rk, dict):
    out("BLOCK:requires_kit must be a JSON object {kit, range}, got %s"
        % type(rk).__name__)
if set(rk.keys()) != {"kit", "range"}:
    out("BLOCK:requires_kit keys must be exactly {kit, range} (got: %s)"
        % (", ".join(sorted(rk.keys())) or "none"))
kit = rk.get("kit")
rng = rk.get("range")
# Grammar single source: provides_kit rules (compat-lint _KIT_TOKEN_RE twin).
if not isinstance(kit, str) or not _TOKEN_RE.match(kit):
    out("BLOCK:requires_kit.kit invalid: %r -- must be a string matching "
        "^[a-z][a-z0-9_-]{0,31}$ (provides_kit grammar reuse)" % (kit,))
for rsv in _RESERVED:
    if kit == rsv:
        out("BLOCK:requires_kit.kit is a framework-reserved name: '%s'" % kit)
if kit == (d.get("id") or None) or kit == (d.get("provides_kit") or None):
    out("BLOCK:requires_kit.kit '%s' equals the pack's own id/provides_kit "
        "(self-dependency)" % kit)
if not isinstance(rng, str):
    out("BLOCK:requires_kit.range must be a string, got %s" % type(rng).__name__)
# Range grammar: same token set as compat-lint _is_semver_range
# (>= > <= < == != + x.y.z; ^/~ unsupported).
tokens = rng.split()
valid_op = re.compile(r'^(>=|>|<=|<|==|!=)\d+\.\d+\.\d+')
if not tokens or any(not valid_op.match(t) for t in tokens):
    out("BLOCK:requires_kit.range is not a valid semver range: %r" % rng)

# --- satisfaction against the instance DOGANY_PACKS record ---
conf = pathlib.Path(sys.argv[2])
if not conf.exists():
    out_dep(".instance.conf not found at %s -- cannot verify kit '%s' is "
            "installed" % (conf, kit))
try:
    conf_lines = conf.read_text(encoding="utf-8").splitlines()
except Exception as e:
    out_dep(".instance.conf unreadable (%s) -- observed nothing, cannot "
            "proceed (DGN-1004)" % e)
packs_line = None
for ln in conf_lines:
    if ln.startswith("DOGANY_PACKS="):
        packs_line = ln
        break
if packs_line is None:
    out_dep("no DOGANY_PACKS line in .instance.conf -- kit '%s' is not "
            "recorded as installed" % kit)
items = [x for x in packs_line.split("=", 1)[1].split(",") if x]
matches = [x for x in items if x.split("@", 1)[0] == kit]
if not matches:
    out_dep("kit '%s' has no <kit>@ entry in DOGANY_PACKS -- required kit "
            "is not installed on this instance (install the kit first)" % kit)
entry = matches[0]
ver_str = entry.split("@", 1)[1] if "@" in entry else ""

def parse_semver(s):
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)', s.strip())
    return tuple(int(x) for x in m.groups()) if m else None

ver = parse_semver(ver_str)
if ver is None:
    out_dep("DOGANY_PACKS entry '%s' has an unparseable version -- cannot "
            "verify the range (unverifiable = no-go, DGN-1004)" % entry)
# Range evaluation: same operator semantics as compat-lint _semver_satisfies.
for t in tokens:
    m = re.match(r'^(>=|>|<=|<|==|!=)(.+)$', t)
    op, cv = m.group(1), parse_semver(m.group(2))
    if cv is None:
        out_dep("requires_kit.range constraint version unparseable: %r" % t)
    if ((op == ">=" and not ver >= cv) or (op == ">" and not ver > cv)
            or (op == "<=" and not ver <= cv) or (op == "<" and not ver < cv)
            or (op == "==" and not ver == cv) or (op == "!=" and not ver != cv)):
        out_dep("installed %s does not satisfy required range '%s'"
                % (entry, rng))
out("PASS:%s (range '%s' satisfied)" % (entry, rng))
PYEOF
}

_rk_verdict="$(_rk_compute_verdict)" || _rk_verdict="BLOCK:requires_kit gate crashed -- fail closed"

# ---------------------------------------------------------------------------
# DGN-1406/1409: requires_kit dependency auto-resolve.
# ---------------------------------------------------------------------------
# A BLOCK verdict carrying the |KIT=/|RANGE= trailer (satisfaction-stage
# only -- see out_dep() above) means: the declaration is well-formed, the
# required kit is simply not (yet) satisfied on this instance. Before
# refusing the install, try to install that kit first. Boundaries (leader-
# fixed; full rationale in the requires-kit auto-resolve design record):
#   1. scope   -- only the exact kit named by requires_kit.kit
#   2. source  -- only packs/catalog.json (pack_catalog_row/pack_coord_
#      resolve, the SAME resolver every other pack uses) -- an unregistered
#      kit BLOCKs with an explicit "미등재" message, nothing is invented
#   3. version -- the single version that source currently carries; if it
#      does not satisfy the declared range, BLOCK (no tag/version search)
#   4. depth   -- one hop: the sub-install runs THIS SAME script, whose
#      kind=kit dispatch already refuses a requires_kit declaration on a kit
#      manifest (see "kind=kit must not declare requires_kit" above), so a
#      kit-of-kit chain BLOCKs by construction, not by extra bookkeeping here
#   5. idempotent -- PASS never reaches this branch (verdict without the
#      trailer), so an already-satisfied install is byte-identical to before
#      this change
#   6. order   -- the sub-install runs to completion (or fails and aborts
#      everything) before the outer pack's OWN install proceeds one step
#   7. no service start -- pack_install never invokes launchd; the compatibility
#      --no-start flag is still passed to nested invocations.
#   8. dry-run -- same predicate, no writes: only prints the plan
#   9. write path -- the sub-install is a full pack_install.sh run, so it
#      upserts DOGANY_PACKS through the existing _packs_upsert_atomic call
#      already on that path; nothing new is added here
_rk_dep_kit="" _rk_dep_range=""
case "$_rk_verdict" in
  BLOCK:*'|KIT='*)
    _rk_dep_kit="${_rk_verdict#*|KIT=}"; _rk_dep_kit="${_rk_dep_kit%%|*}"
    _rk_dep_range="${_rk_verdict##*|RANGE=}"
    _rk_verdict="${_rk_verdict%%|KIT=*}"
    ;;
esac

# _crew_kit_version <kit> -- DGN-1682 item 4: the kit version the EXISTING
# crew runs, read from the crew's own sources of truth (no new registry):
# members = the other estate instances whose database/<kit>.db resolves to
# the crew-shared DB (source 3 of the DGN-1509 3-source cross-validation,
# the same scan _rk_scope_gate / the lockstep guard use), version = each
# member's DOGANY_PACKS <kit>@<ver> record (source 2 -- the exact field
# update_plan.sh compares when it ABORTs a split crew). This instance itself
# is excluded: the question is what the crew it is joining/staying in runs.
# Sets:
#   CREW_KIT_STATE  none       -- no crew DB, or no other member
#                   uniform    -- every other member records the same version
#                   split      -- other members already disagree
#                   unrecorded -- a member mounts the DB with no <kit>@ record
#   CREW_KIT_V      the uniform version (uniform only)
#   CREW_KIT_PEERS  "name=ver, ..." for messages
# Returns 1 when the estate cannot be scanned (callers fail closed).
_crew_kit_version() {
  local kit="$1"
  local shared_home="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
  local crew_db="$shared_home/crews/$kit/$kit.db"
  local crew_phys disc scan self_phys line name root other phys pv
  CREW_KIT_STATE="none"; CREW_KIT_V=""; CREW_KIT_PEERS=""
  [[ -e "$crew_db" || -L "$crew_db" ]] || return 0
  crew_phys="$(_kit_resolve_link "$crew_db")" || return 0
  disc="$SCRIPT_DIR/lib/discover_instances.sh"
  [[ -f "$disc" && -r "$disc" ]] || return 1
  # shellcheck source=lib/discover_instances.sh
  . "$disc"
  scan="$(_discover_instances)" || return 1
  self_phys="$(cd -P "$ROOT" 2>/dev/null && pwd)" || self_phys="$ROOT"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    name="${line%%:*}"
    root="${line#*:}"
    [[ "$root" == "$self_phys" ]] && continue
    other="$root/database/$kit.db"
    [[ -e "$other" || -L "$other" ]] || continue
    phys="$(_kit_resolve_link "$other")" || continue
    [[ "$phys" == "$crew_phys" ]] || continue
    pv="$(sed -n 's/^DOGANY_PACKS=//p' "$root/.instance.conf" 2>/dev/null | head -1 \
      | tr ',' '\n' | awk -F@ -v id="$kit" '$1 == id {print substr($0, length(id)+2)}' | head -1)"
    CREW_KIT_PEERS="${CREW_KIT_PEERS:+$CREW_KIT_PEERS, }$name=${pv:-<none>}"
    if [[ -z "$pv" ]]; then
      CREW_KIT_STATE="unrecorded"
    elif [[ "$CREW_KIT_STATE" == "none" ]]; then
      CREW_KIT_STATE="uniform"; CREW_KIT_V="$pv"
    elif [[ "$CREW_KIT_STATE" == "uniform" && "$pv" != "$CREW_KIT_V" ]]; then
      CREW_KIT_STATE="split"
    fi
  done <<< "$scan"
  [[ "$CREW_KIT_STATE" == "uniform" ]] || CREW_KIT_V=""
  return 0
}

# _rk_scope_gate <kit> <orig_block_reason> -- DGN-1461 effect-scope check
# for the auto-resolve path. Returns 0 when the kit's crew-shared DB has no
# OTHER consumer among the discovered estate instances; _fails (pre-reject,
# process exit) when one exists. Fail-closed on an unusable estate scan: an
# unbounded effect scope is never assumed harmless.
_rk_scope_gate() {
  local kit="$1" orig="$2"
  local shared_home="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
  local crew_db="$shared_home/crews/$kit/$kit.db"
  local crew_phys disc scan self_phys line name root other phys others=""
  [[ -e "$crew_db" || -L "$crew_db" ]] || return 0
  crew_phys="$(_kit_resolve_link "$crew_db")" || return 0
  disc="$SCRIPT_DIR/lib/discover_instances.sh"
  [[ -f "$disc" && -r "$disc" ]] \
    || _fail "requires_kit gate: $orig (SCOPE GATE: instance discovery library missing ($disc) -- cannot bound the effect scope of auto-installing kit '$kit', fail-closed; DGN-1461)"
  # shellcheck source=lib/discover_instances.sh
  . "$disc"
  scan="$(_discover_instances)" \
    || _fail "requires_kit gate: $orig (SCOPE GATE: HOME estate scan failed -- cannot rule out other consumers of the crew-shared '$kit' DB, fail-closed; DGN-1461)"
  self_phys="$(cd -P "$ROOT" 2>/dev/null && pwd)" || self_phys="$ROOT"
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    name="${line%%:*}"
    root="${line#*:}"
    [[ "$root" == "$self_phys" ]] && continue
    other="$root/database/$kit.db"
    [[ -e "$other" || -L "$other" ]] || continue
    phys="$(_kit_resolve_link "$other")" || continue
    [[ "$phys" == "$crew_phys" ]] || continue
    others="${others:+$others, }$name"
  done <<< "$scan"
  if [[ -n "$others" ]]; then
    # Owner ruling 2026-09-18 ("데이터는 그냥 같이쓰는거라고오오"): the
    # crew-shared-data CONSENT QUESTION this stop used to send to the owner
    # (gates.json requires-kit.scope user_hint_ko, "같이 쓰게 할까요?") is
    # DELETED -- choosing the pack IS the consent, and the mint pipeline
    # carries that choice as DOGANY_CREW_JOIN_ACK=1 (the same wire
    # _kit_crew_join_gate already reads; SKILL.md step 6c). With the ack on
    # record the join's effect scope IS the request's consented scope, so
    # this gate proceeds. What the ack does NOT waive -- the parts of this
    # gate's old job that were never the consent question -- still stand
    # elsewhere: the crew-lockstep guard (DGN-1436) refuses to ADVANCE the
    # shared DB past a lagging member. DGN-1869 makes the successful kit
    # install register this member after its physical join.
    if [[ "${DOGANY_CREW_JOIN_ACK:-0}" == "1" ]]; then
      _log "requires_kit gate: SCOPE GATE (DGN-1461): crew join ACK on record (DOGANY_CREW_JOIN_ACK=1) -- auto-resolving kit '$kit' joins the crew-shared DB ($crew_db) consumed by: $others; the join was consented by the caller's own request (choosing the pack IS the consent, owner ruling 2026-09-18), proceeding. Lockstep (DGN-1436) is NOT waived; crew.conf registration follows the successful install (DGN-1869)"
      return 0
    fi
    # No ack (direct operator install, not a consented flow): the effect
    # range of this single-instance request would silently exceed it --
    # keep refusing, but as an OPERATOR stop (_fail), not an owner screen.
    # DGN-1509 S8: this branch only ever fires for an instance that is NOT
    # yet a member of that crew (kit reaches _rk_autoresolve_kit only when
    # it is not already installed here) -- "run a crew-scoped update that
    # names every member" is not a path this caller can take (update_apply.sh
    # only operates on registered crew.conf members, and this requester is
    # not one). A consented install now completes registration itself
    # after the physical join (DGN-1869).
    _fail "requires_kit gate: SCOPE GATE (DGN-1461): auto-resolving required kit '$kit' for '$PACK_ID' would join/advance the crew-shared DB ($crew_db) already consumed by other instance(s): $others -- the effect range would exceed this single-instance request; refusing BEFORE any change, not auto-expanding. This install cannot silently join a multi-member crew as a side effect of resolving a dependency. A consented flow (mint pipeline) passes DOGANY_CREW_JOIN_ACK=1 on THIS install and proceeds in one call"
  fi
  return 0
}

# _rk_autoresolve_kit <kit> <range> <orig_block_reason>
# Either installs the kit and returns 0, or calls _fail (process exit) --
# there is no other return path, so every caller below only has to handle
# the success case.
_rk_autoresolve_kit() {
  local kit="$1" range="$2" orig="$3"
  local row dir mf ver chk sub_out sub_rc=0

  # Depth guard (boundary 4, made explicit 2026-09-12). One level only.
  # The implicit guard -- "kind=kit must not declare requires_kit" -- holds
  # today, but it lives in a different check that a future contract revision
  # could relax; a runaway install chain is not an acceptable failure mode to
  # leave resting on someone else's invariant. This counter is carried to the
  # recursive call through the environment and refuses a second hop outright.
  if [[ "${DOGANY_RK_DEPTH:-0}" -ge 1 ]]; then
    _fail "requires_kit gate: $orig (auto-resolve refused -- already resolving a kit one level up; chain: '$PACK_ID' -> '$kit'. Install '$kit' manually first, then retry)"
  fi

  row="$(pack_catalog_row "$CATALOG" "$kit")" || row="null"
  if [[ -z "$row" || "$row" == "null" ]]; then
    _fail "requires_kit gate: $orig (kit '$kit' 미등재라서 자동 해소 불가 -- no row in the catalog ($CATALOG); register it in packs/catalog.json / pack-sources.conf, or install it manually first)"
  fi
  # DGN-1818: sibling checkout, else the public-install pack-src clone
  # (pack_coords.sh pack_row_dir; the clone is made by a kit install).
  dir="$(pack_row_dir "$CATALOG" "$row")"
  # DGN-1859: on a public install nothing has cloned the kit yet (a kitless
  # main never ran a kit install), so pack_row_dir finds no pack-src clone.
  # Fetch it from the row's source_url exactly like the requested pack's own
  # source above (same fetcher, same dry-run private tmp); a sibling or an
  # existing clone still wins.
  if [[ -z "$dir" || ! -f "$dir/pack-manifest.json" ]]; then
    local _rk_url _rk_home
    _rk_url="$(pack_row_field "$row" source_url)"
    if [[ -n "$_rk_url" ]]; then
      _rk_home="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
      if [[ "$DRY" -eq 1 ]]; then
        _rk_home="$(mktemp -d "${TMPDIR:-/tmp}/pack-src-dry.XXXXXX")" || _fail "pack source: mktemp failed"
      fi
      dir="$(DOGANY_SHARED_HOME="$_rk_home" pack_source_fetch "$kit" "$_rk_url" "")" \
        || _fail "requires_kit gate: $orig (kit '$kit': cannot fetch from $_rk_url -- see the reason above)"
      _log "requires_kit gate: kit source: $kit fetched from $_rk_url -> $dir ($(git -C "$dir" describe --tags --exact-match 2>/dev/null || echo '?')) (DGN-1859)"
    fi
  fi
  if [[ -z "$dir" || ! -f "$dir/pack-manifest.json" ]]; then
    _fail "requires_kit gate: $orig (kit '$kit' 미등재라서 자동 해소 불가 -- catalog row has no reachable package_dir/manifest (looked at: '${dir:-<empty>}'); register a valid source first)"
  fi
  mf="$dir/pack-manifest.json"

  chk="$("$PACK_PY3" -c "
import json, sys
d = json.load(open(sys.argv[1]))
print('OK' if d.get('kind') == 'kit' and d.get('provides_kit') == sys.argv[2] else 'MISMATCH')
" "$mf" "$kit" 2>/dev/null || echo "MISMATCH")"
  [[ "$chk" == "OK" ]] || _fail "requires_kit gate: $orig (kit '$kit' 미등재라서 자동 해소 불가 -- catalog row resolves to a manifest that does not provide it (kind/provides_kit mismatch at $mf))"

  ver="$(pack_row_field "$row" pack_version)"
  if [[ -z "$ver" ]]; then
    ver="$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get('pack_version'); print(v if isinstance(v,str) else '')" "$mf" 2>/dev/null || echo "")"
  fi
  [[ -n "$ver" ]] || _fail "requires_kit gate: $orig (kit '$kit' source declares no pack_version, neither catalog nor manifest at $mf -- unverifiable version, auto-resolve refused)"

  # DGN-1461 SCOPE GATE: the effect range of an install must not exceed its
  # request range. This call names ONE instance ($ROOT); auto-resolving a
  # kit is a CREW-joining act (the kit DB is crew property -- see
  # _kit_crew_db_link_ensure), and its migrate step can advance a DB other
  # instances read. If the crew-shared DB for '$kit' already has consumers
  # among the OTHER discovered estate instances, this single-instance
  # request would silently touch members it never named -- refuse BEFORE
  # any write (dry-run included: judgment must be identical), never
  # auto-expand the scope. A crew DB with no other consumer (or none at
  # all) keeps today's behavior: joining affects only the requester.
  _rk_scope_gate "$kit" "$orig"

  # DGN-1682 item 4: crew kit version coherence. The scope gate above only
  # decides WHETHER this instance may join/advance the crew; it never asked
  # WHICH version the crew runs, so a consented join installed the catalog's
  # version into one member (rehearsal 2 S1b / S3-reverse: crew split
  # 2.27.0 / 2.26.0, after which update_plan.sh ABORTs every update). Rule:
  # a crew member never gets a kit version the rest of the crew does not
  # run. Crew on V -> install exactly V (from its tag in the kit source) if
  # the requires_kit range accepts V; otherwise refuse BEFORE any write --
  # the crew's kit is upgraded first, by a crew update that moves every
  # member together.
  local src_tag="" v_tag v_top v_rel v_ex=""
  if ! _crew_kit_version "$kit"; then
    _fail "requires_kit gate: $orig (CREW KIT VERSION: HOME estate scan failed -- cannot read which '$kit' version the existing crew runs, refusing before any change, fail-closed; DGN-1682)"
  fi
  case "$CREW_KIT_STATE" in
    split)
      _fail "requires_kit gate: $orig (CREW KIT VERSION: the existing '$kit' crew is already split across versions ($CREW_KIT_PEERS) -- installing any version here would widen the split; bring the crew to ONE kit version first (raise an update offer; owner picks the crew update in /update), then retry '$PACK_ID'. Refused before any change (DGN-1682)" ;;
    unrecorded)
      _fail "requires_kit gate: $orig (CREW KIT VERSION: a member of the '$kit' crew mounts the shared DB with no '$kit@' DOGANY_PACKS record ($CREW_KIT_PEERS) -- the crew's version cannot be read, refusing before any change, fail-closed (DGN-1682)" ;;
    uniform)
      if [[ "$CREW_KIT_V" == "$ver" ]]; then
        _log "requires_kit gate: crew kit version: existing '$kit' crew runs $CREW_KIT_V ($CREW_KIT_PEERS) == source version -- coherent (DGN-1682)"
      elif "$PACK_PY3" "$SCRIPT_DIR/lib/semver_range.py" satisfies "$CREW_KIT_V" "$range" >/dev/null 2>&1; then
        v_tag="pack/$kit/v$CREW_KIT_V"
        v_top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" || v_top=""
        if [[ -z "$v_top" ]] || ! git -C "$v_top" rev-parse -q --verify "refs/tags/$v_tag" >/dev/null 2>&1; then
          _fail "requires_kit gate: $orig (CREW KIT VERSION: the existing '$kit' crew runs $CREW_KIT_V ($CREW_KIT_PEERS) and '$PACK_ID' accepts it (range '$range'), but the kit source ($dir) has no tag $v_tag to install that exact version from -- refusing to install $ver into one member (that splits the crew). Restore the tag or upgrade the crew's kit first; refused before any change (DGN-1682)"
        fi
        _log "requires_kit gate: crew kit version: existing '$kit' crew runs $CREW_KIT_V ($CREW_KIT_PEERS); source carries $ver -- installing the CREW's version $CREW_KIT_V from $v_tag instead (accepted by '$range'; DGN-1682)"
        src_tag="$v_tag"
        ver="$CREW_KIT_V"
        if [[ "$DRY" -ne 1 ]]; then
          v_rel="$("$PACK_PY3" -c 'import os,sys; print(os.path.relpath(os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])))' "$dir" "$v_top")" \
            || _fail "requires_kit gate: $orig (CREW KIT VERSION: cannot locate $dir inside $v_top, fail-closed)"
          v_ex="$(mktemp -d "${TMPDIR:-/tmp}/rk-crewkit.XXXXXX")" \
            || _fail "requires_kit gate: $orig (CREW KIT VERSION: cannot create an extraction dir for $v_tag, fail-closed)"
          if ! git -C "$v_top" archive --format=tar "refs/tags/$v_tag" | tar -xf - -C "$v_ex"; then
            rm -rf "$v_ex"
            _fail "requires_kit gate: $orig (CREW KIT VERSION: extracting $v_tag from $v_top failed, fail-closed; DGN-1682)"
          fi
          dir="$v_ex/$v_rel"
          [[ -f "$dir/pack-manifest.json" ]] || { rm -rf "$v_ex"; _fail "requires_kit gate: $orig (CREW KIT VERSION: $v_tag has no pack-manifest.json at '$v_rel', fail-closed; DGN-1682)"; }
        fi
      elif "$PACK_PY3" "$SCRIPT_DIR/lib/semver_range.py" satisfies "$ver" "$range" >/dev/null 2>&1 \
           && [[ "$("$PACK_PY3" "$SCRIPT_DIR/lib/semver_range.py" compare "$ver" "$CREW_KIT_V" 2>/dev/null)" == "1" ]]; then
        # The owner's path (rehearsal 2 S1b): the crew is BEHIND what this
        # pack needs. The only coherent move is a crew-wide kit upgrade
        # first -- a planned owner decision, not a fault.
        _gate_stop kit.crew-version "requires_kit gate: $orig (CREW KIT VERSION: '$PACK_ID' needs '$kit' in range '$range', but the existing '$kit' crew runs $CREW_KIT_V ($CREW_KIT_PEERS), outside that range. Installing $ver into this one member would split the crew (update_plan.sh then ABORTs every update). Upgrade the crew's kit first -- a crew update that moves every member to $ver together (raise an update offer; owner picks it in /update) -- then retry '$PACK_ID'. Refused before any change (DGN-1682)"
      else
        _fail "requires_kit gate: $orig (CREW KIT VERSION: the existing '$kit' crew runs $CREW_KIT_V ($CREW_KIT_PEERS), which does not satisfy '$PACK_ID' range '$range', and the kit source version $ver is no coherent way forward either -- refusing to split the crew; resolve the crew's kit version first. Refused before any change (DGN-1682)"
      fi
      ;;
  esac

  if ! "$PACK_PY3" "$SCRIPT_DIR/lib/semver_range.py" satisfies "$ver" "$range" >/dev/null 2>&1; then
    _fail "requires_kit gate: $orig (kit '$kit' source resolves to version $ver, which does not satisfy required range '$range' -- no other version is available from this source, auto-resolve refused)"
  fi

  # DGN-1682 item 2: the caller's --untagged-local declaration covers the
  # dependency it drags in. Without forwarding it, the sub-install hit the
  # source-tag gate on the same untagged work tree the caller had already
  # declared, and the whole mint BLOCKed (rehearsal 2 S1). Forwarded
  # verbatim (with the chain appended for the receipt's audit line); a
  # tag-backed caller forwards nothing and the sub-install keeps judging its
  # own tag. Never combined with --source-tag (none is passed below).
  local -a sub_decl=()
  if [[ -n "$src_tag" ]]; then
    # crew-version payload is tag-backed by construction (item 4 above);
    # --source-tag and --untagged-local are contradictory, pass the tag only.
    sub_decl=(--source-tag "$src_tag")
  elif [[ -n "$UNTAGGED_REASON" ]]; then
    sub_decl=(--untagged-local "$UNTAGGED_REASON [inherited: auto-resolve for $PACK_ID]")
  fi

  if [[ "$DRY" -eq 1 ]]; then
    _log "requires_kit gate: auto-resolve PLAN (dry-run, no writes) -- would install required kit '$kit'@$ver (satisfies '$range') from $dir before '$PACK_ID'${src_tag:+ (crew version, from $src_tag)}$([[ -z "$src_tag" && -n "$UNTAGGED_REASON" ]] && echo " (--untagged-local declaration forwarded to the kit install)")"
    return 0
  fi

  _log "requires_kit gate: auto-resolve -- installing required kit '$kit'@$ver (satisfies '$range') from $dir before '$PACK_ID'"
  if [[ -z "$src_tag" && -n "$UNTAGGED_REASON" ]]; then
    _log "requires_kit gate: auto-resolve -- forwarding the caller's --untagged-local declaration to the kit install (reason: $UNTAGGED_REASON)"
  fi
  sub_out="$(DOGANY_RK_DOMAIN_PACK="$PACK_ID" DOGANY_RK_DEPTH=$(( ${DOGANY_RK_DEPTH:-0} + 1 )) \
      bash "$SELF" "$SLUG" "$ROOT" --pack "$kit" --pack-dir "$dir" \
      --catalog "$CATALOG" --model "$MODEL_OPT" \
      ${sub_decl[@]+"${sub_decl[@]}"} --no-start --no-state 2>&1)" || sub_rc=$?
  while IFS= read -r _sl; do _log "  [autoresolve:$kit] $_sl"; done <<< "$sub_out"
  [[ -z "$v_ex" ]] || rm -rf "$v_ex"
  if [[ "$sub_rc" -ne 0 ]]; then
    _fail "requires_kit gate: $orig (auto-resolve install of required kit '$kit' FAILED, rc=$sub_rc -- chain: '$PACK_ID' -> '$kit'; see the [autoresolve:$kit] lines above for the kit's own failure; '$PACK_ID' was NOT installed, no partial install)"
  fi
  _log "requires_kit gate: auto-resolve -- kit '$kit'@$ver installed"
  return 0
}

_rk_final_logged=0
if [[ -n "$_rk_dep_kit" ]]; then
  if _rk_autoresolve_kit "$_rk_dep_kit" "$_rk_dep_range" "${_rk_verdict#BLOCK:}"; then
    if [[ "$DRY" -eq 1 ]]; then
      _log "requires_kit gate: SKIP (auto-resolve plan only, dry-run -- kit '$_rk_dep_kit' not installed, no writes)"
      _rk_final_logged=1
    else
      # Re-derive against the now-updated .instance.conf -- same predicate,
      # no second implementation. Strip a trailer again defensively (a fresh
      # BLOCK here would mean the just-installed kit still doesn't satisfy
      # the range, which the version check above should already have ruled
      # out; fail-closed rather than assume).
      _rk_verdict="$(_rk_compute_verdict)" || _rk_verdict="BLOCK:requires_kit gate crashed -- fail closed"
      case "$_rk_verdict" in BLOCK:*'|KIT='*) _rk_verdict="${_rk_verdict%%|KIT=*}" ;; esac
    fi
  fi
fi

if [[ "$_rk_final_logged" -eq 0 ]]; then
  case "$_rk_verdict" in
    SKIP:*) _log "requires_kit gate: SKIP -- ${_rk_verdict#SKIP:}" ;;
    PASS:*)
      _log "requires_kit gate: PASS -- ${_rk_verdict#PASS:}"
      # DGN-1456: the satisfaction verdict above trusts the DOGANY_PACKS
      # ledger line alone -- exactly the record that lied on the measured
      # ghost entry (main agent pack_health@0.10.0, payload never delivered). Consult
      # the install evidence for the required kit through the SAME reader
      # update_plan/update_apply use: a ledger claim contradicted by the
      # physical evidence (MISMATCH), an interrupted install (INCOMPLETE) or
      # a broken checker (CHECK_ERROR) is a refusal, not a pass. UNKNOWN
      # (pre-receipt legacy install trace) proceeds loudly -- receipts only
      # exist for installs made after this contract landed.
      _rk_pass_entry="${_rk_verdict#PASS:}"; _rk_pass_entry="${_rk_pass_entry%% *}"
      _rk_pass_kit="${_rk_pass_entry%%@*}"
      _rk_pass_ver="${_rk_pass_entry#*@}"
      _rk_ev="$(evidence_consume "$ROOT" "$_rk_pass_kit" "$_rk_pass_ver")"
      _rk_ev_v="${_rk_ev%%|*}"; _rk_ev_d="${_rk_ev#*|}"
      case "$_rk_ev_v" in
        VERIFIED) _log "requires_kit gate: install evidence VERIFIED for $_rk_pass_entry ($_rk_ev_d)" ;;
        UNKNOWN)  _log "requires_kit gate: WARN -- install evidence UNKNOWN for $_rk_pass_entry ($_rk_ev_d) -- ledger satisfaction stands but is unverified (legacy grace)" ;;
        *) _fail "requires_kit gate: install evidence $_rk_ev_v for required kit $_rk_pass_entry -- $_rk_ev_d. The ledger entry is not accepted as proof of install (DGN-1456 fail-closed; re-install the kit to issue a receipt)" ;;
      esac
      unset _rk_pass_entry _rk_pass_kit _rk_pass_ver _rk_ev _rk_ev_v _rk_ev_d
      ;;
    BLOCK:*) _fail "requires_kit gate: ${_rk_verdict#BLOCK:} (DGN-681 S4b-1 install-time satisfaction check; fail-closed, no warn lane)" ;;
    *) _fail "requires_kit gate: unrecognized verdict '$_rk_verdict' -- fail closed" ;;
  esac
fi
unset _rk_dep_kit _rk_dep_range _rk_final_logged

# ---------------------------------------------------------------------------
# requires_modules reference gate (DGN-1391) -- PRE-PAYLOAD-COPY
# ---------------------------------------------------------------------------
# A pack names the modules it builds on:
#
#   "requires_modules": ["workout", "diet"]
#
# SEMANTICS: REFERENCE, never an ownership claim. Naming a module gives the
# pack no right to own, migrate or version that module's tables -- the kit
# owns them. The key is the sibling of requires_kit (same verb, same
# direction, same non-ownership semantics), which is why it is spelled this
# way and not as a "provides" claim: the ownership spelling was measured
# false for behavior packs (db_lane=false, zero tables owned) and was
# deleted; it does not come back.
#
# WHAT THIS GATE DOES, AND NOTHING ELSE: every named module must exist in the
# module ROSTER of the kit named by requires_kit. No catalog consumption, no
# product-taxonomy consumption, no runtime module registry (none exists).
#
# ROSTER SOURCE -- a gap stated out loud (DGN-1391): a kit's modules are not
# machine-readable anywhere today. The only roster that exists is prose in a
# governance document outside this repo, which an installer cannot read and
# must not depend on. So the roster is read from the one place that owns the
# fact and that the installer can already address -- the required kit's own
# manifest:
#
#   "modules": ["<module token>", ...]
#
# located through the catalog coordinate (lib/pack_coords.sh -- the same
# resolver the publisher uses; no second coordinate rule). Two consequences
# are kept LOUD rather than hidden: a kit that declares no roster cannot
# satisfy any requires_modules reference, and an instance that cannot reach
# the kit's manifest gets a BLOCK, not a pass (unverifiable = no-go,
# DGN-1004). Capturing the roster on the instance at kit-install time (the
# config/packs/<id>.requires_framework precedent) is what would remove the
# reachability condition -- that is a separate change, not this one.
#
# Verdict table (ALL fail-closed, NO warn lane):
#   legacy pack (no contract_version AND kind != kit)  -> SKIP
#   requires_modules absent                            -> SKIP (install is
#                                                         byte-identical to
#                                                         before this gate)
#   form damage (non-array / non-string entry / token
#     grammar / duplicate / empty array)               -> BLOCK
#   requires_modules declared without requires_kit     -> BLOCK
#   kit catalog row / coordinate / manifest unreachable-> BLOCK
#   kit declares no module roster                      -> BLOCK
#   kit roster malformed                               -> BLOCK
#   named module absent from the roster                -> BLOCK
#   every named module present                         -> PASS + counts
#
# Token grammar is the kit-token core (lib/pack_token_grammar.sh) reused, not
# a new grammar. The framework-reserved NAME list that accompanies it is
# deliberately NOT applied here: that list defends payload path surfaces
# against impersonation, and a module token addresses no path in this change.
_rm_verdict="$("$PACK_PY3" - "$MANIFEST" "$PACK_TOKEN_CORE" <<'PYEOF'
import json, re, sys

def out(v):
    print(v)
    sys.exit(0)

# Anchored with \Z, not $: a python $ also matches before a trailing newline
# (DGN-1018 5-B class defect) -- see lib/pack_token_grammar.sh ANCHOR NOTE.
_TOKEN_RE = re.compile('^' + sys.argv[2] + r'\Z')

try:
    d = json.load(open(sys.argv[1]))
    if not isinstance(d, dict):
        raise ValueError("manifest top-level JSON is not an object")
except Exception as e:
    out("BLOCK:pack-manifest.json unreadable (%s)" % e)

contract = d.get("contract_version")
kind = d.get("kind") or ""

# Legacy-grace mirror (same boundary as the requires_kit gate above).
if (contract is None or contract == "") and kind != "kit":
    out("SKIP:legacy pack (no contract_version) -- requires_modules gate not "
        "applicable")

if "requires_modules" not in d:
    out("SKIP:requires_modules absent -- no module reference declared")

rm = d["requires_modules"]
if not isinstance(rm, list):
    out("BLOCK:requires_modules must be a JSON array of module names, got %s"
        % type(rm).__name__)
if not rm:
    out("BLOCK:requires_modules is an empty array -- a declared reference must "
        "name at least one module (drop the key to declare no reference)")
wanted = []
for m in rm:
    if not isinstance(m, str) or not _TOKEN_RE.match(m):
        out("BLOCK:requires_modules entry invalid: %r -- must be a string "
            "matching ^%s$ (kit token grammar reuse)" % (m, sys.argv[2]))
    if m in wanted:
        out("BLOCK:requires_modules names '%s' more than once -- duplicate "
            "references refused" % m)
    wanted.append(m)

rk = d.get("requires_kit")
kit = rk.get("kit") if isinstance(rk, dict) else None
if not isinstance(kit, str) or not kit:
    out("BLOCK:requires_modules declared without a usable requires_kit.kit -- "
        "a module reference is resolved against the required kit's roster, so "
        "the kit has to be declared first")

out("OK:%s|%s" % (kit, " ".join(wanted)))
PYEOF
)" || _rm_verdict="BLOCK:requires_modules gate crashed -- fail closed"

if [[ "$_rm_verdict" == OK:* ]]; then
  _rm_body="${_rm_verdict#OK:}"
  _rm_kit="${_rm_body%%|*}"
  _rm_list="${_rm_body#*|}"
  _rm_row="$(pack_catalog_row "$CATALOG" "$_rm_kit")" || _rm_row="null"
  if [[ -z "$_rm_row" || "$_rm_row" == "null" ]]; then
    _rm_verdict="BLOCK:required kit '$_rm_kit' has no row in the catalog ($CATALOG) -- its module roster cannot be located (unverifiable = no-go)"
  else
    _rm_kit_dir="$(pack_row_dir "$CATALOG" "$_rm_row")"
    if [[ -z "$_rm_kit_dir" ]]; then
      _rm_verdict="BLOCK:required kit '$_rm_kit' catalog row carries no package_dir -- its module roster cannot be located"
    elif [[ ! -f "$_rm_kit_dir/pack-manifest.json" ]]; then
      _rm_verdict="BLOCK:required kit '$_rm_kit' manifest not found at $_rm_kit_dir/pack-manifest.json -- the module roster is unreachable from this instance (unverifiable = no-go)"
    else
      # Word splitting on $_rm_list is intended: the entries passed here are
      # the grammar-validated tokens above (no whitespace, no glob chars).
      # shellcheck disable=SC2086
      _rm_verdict="$("$PACK_PY3" - "$_rm_kit_dir/pack-manifest.json" "$_rm_kit" $_rm_list <<'PYEOF'
import json, sys

def out(v):
    print(v)
    sys.exit(0)

kit_manifest, kit = sys.argv[1], sys.argv[2]
wanted = sys.argv[3:]

try:
    d = json.load(open(kit_manifest))
    if not isinstance(d, dict):
        raise ValueError("manifest top-level JSON is not an object")
except Exception as e:
    out("BLOCK:required kit '%s' manifest unreadable at %s (%s)"
        % (kit, kit_manifest, e))

if "modules" not in d:
    out("BLOCK:required kit '%s' declares no module roster ('modules' key "
        "absent from %s) -- every named module is therefore unverifiable, and "
        "an unverifiable reference is refused. Fix: the kit declares the "
        "modules it contains, then the reference can be checked."
        % (kit, kit_manifest))

roster = d["modules"]
if (not isinstance(roster, list)
        or any((not isinstance(x, str) or not x) for x in roster)):
    out("BLOCK:required kit '%s' module roster is malformed -- 'modules' must "
        "be a JSON array of module names (%s)" % (kit, kit_manifest))

missing = [m for m in wanted if m not in roster]
if missing:
    out("BLOCK:module(s) not in the roster of kit '%s': %s -- kit roster "
        "(%d): %s" % (kit, ", ".join(missing), len(roster),
                      ", ".join(roster) or "(empty)"))

# Population is always printed, never just the verdict (DGN-1142 3.1).
out("PASS:%d module reference(s) checked against kit '%s' roster of %d: %s"
    % (len(wanted), kit, len(roster), ", ".join(wanted)))
PYEOF
)" || _rm_verdict="BLOCK:requires_modules roster check crashed -- fail closed"
    fi
  fi
fi

case "$_rm_verdict" in
  SKIP:*) _log "requires_modules gate: SKIP -- ${_rm_verdict#SKIP:}" ;;
  PASS:*) _log "requires_modules gate: PASS -- ${_rm_verdict#PASS:}" ;;
  BLOCK:*) _fail "requires_modules gate: ${_rm_verdict#BLOCK:} (DGN-1391 install-time module reference check; fail-closed, no warn lane)" ;;
  *) _fail "requires_modules gate: unrecognized verdict '$_rm_verdict' -- fail closed" ;;
esac
unset _rm_body _rm_kit _rm_list _rm_row _rm_kit_dir

# ---------------------------------------------------------------------------
# Cross-pack skill collision gate (DGN-1143) -- PRE-PAYLOAD-COPY
# ---------------------------------------------------------------------------
# The install-side twin of pack_publish.sh GATE (e): the incoming pack's
# shipped skills-bundle names must not collide with a skill shipped by any
# pack already RECORDED on this instance (config/packs/<id>.requires_framework
# -- the same records the framework-update compat gate reads), unless every
# colliding pack's manifest declares the same winner. Without this, install
# order silently decides which copy of a skill the instance runs (관찰 1:
# diet-log/workout-log, lifekit x health-trainer, 3-way diverged) -- the
# publish-side gate cannot catch it because instances install from package_dir
# WORKING TREES and from historical tags, not only from freshly-gated
# publishes. Verdict + counts: lib/skill_collision_check.sh (shared single
# predicate; the `.`-source line below is also the static E2b reference that
# keeps the lib exported). Fail-closed: verdict script missing = broken
# framework tree =
# FATAL (it ships in the same export as this installer, unlike the legacy
# compat-lint fail-open lane whose excuse was instances predating the file);
# a recorded pack that no longer resolves via the catalog = FATAL (stale
# record -- e.g. a dec-145 rename without record migration -- loud, never a
# silent skip). The checker itself prints the n/a lanes (incoming pack ships
# no skills / no other pack recorded) with counts, per DGN-1142 §3.1.
_SKILLX_LIB="$SCRIPT_DIR/lib/skill_collision_check.sh"
[[ -f "$_SKILLX_LIB" ]] \
  || _fail "skill-collision gate: verdict script missing ($_SKILLX_LIB) -- fail-closed (DGN-1143; the lib ships with this installer, absence means a broken framework tree)"
# Sourced, not bash-invoked: this `.` line is the static reference publish.sh
# gate E2b recognizes (E2 pass B keywords: source/./python3) -- the earlier
# `bash "$VAR"` subprocess form was invisible to the scanner and E2b blocked
# the lib as an unreferenced export (DGN-1143 재수정).
# shellcheck source=lib/skill_collision_check.sh
. "$_SKILLX_LIB"
_sx_restrict=""
if [[ -d "$ROOT/config/packs" ]]; then
  while IFS= read -r _sx_rec; do
    _sx_rec="$(basename "$_sx_rec")"; _sx_rec="${_sx_rec%.requires_framework}"
    [[ "$_sx_rec" == "$PACK_ID" ]] && continue
    _sx_restrict="${_sx_restrict:+$_sx_restrict,}$_sx_rec"
  done < <(find "$ROOT/config/packs" -maxdepth 1 -name '*.requires_framework' -type f 2>/dev/null | sort)
fi
if [[ -n "$_sx_restrict" ]]; then
  _sx_out=""; _sx_rc=0
  _sx_out="$(skill_collision_check --self "$PACK_ID=$PACKAGE_DIR" --catalog "$CATALOG" --restrict "$_sx_restrict" 2>&1)" || _sx_rc=$?
  if [[ "$_sx_rc" -ne 0 ]]; then
    while IFS= read -r _sx_l; do _log "  $_sx_l"; done <<< "$_sx_out"
    _fail "skill-collision gate: incoming pack '$PACK_ID' vs installed [{$_sx_restrict}] -- BLOCKED (DGN-1143; declare an agreed winner in every colliding pack's skills[] block, or stop shipping the duplicate)"
  fi
  _log "skill-collision gate: $(printf '%s\n' "$_sx_out" | tail -1)"
else
  _log "skill-collision gate: n/a -- no other pack recorded on this instance (config/packs/*.requires_framework empty; nothing to collide -- 대상 없음)"
fi

# ---------------------------------------------------------------------------
# KIT/PACK CONTRACT-CLASS INSTALL PATH (DGN-803 LS-4; DGN-1018 §5-B)
# Dispatches early when kind=kit OR (kind=pack AND contract_version present);
# handles the contract payload layout (no reference_slug / categories
# contract -- uses payload_root directly). kind=pack runs the BEHAVIOR-PACK
# PROFILE: same pipeline with the kit-exclusive steps (kit_core / db-init /
# migrate / mirror / units engrave) class-gated OFF and the service surface
# keyed by service_namespace instead of KIT_NAME.
# Execution is fully self-contained, exits 0 on success (does not fall
# through to the agent/module path below).
# ---------------------------------------------------------------------------

# Extractor: parse EXPECTED_USER_VERSION = <N> from a kit core .py file
# (database/<kit>.py -- lifekit.py for the lifekit kit, DGN-1003 generalized).
# Ported verbatim from update.sh drift_guard_file machinery (DGN-249).
# Prints the integer on stdout; exits non-zero on parse failure.
_kit_extract_ver_kit_py() {
  local f="$1"
  [ -f "$f" ] || return 1
  "$PACK_PY3" -c "
import re, sys
txt = open(sys.argv[1]).read()
m = re.search(r'^EXPECTED_USER_VERSION\s*=\s*([0-9]+)', txt, re.MULTILINE)
if not m: sys.exit(1)
print(m.group(1))
" "$f" 2>/dev/null
}

# Extractor: parse MIN_USER_VERSION = <N> from a sdk_bridge.py file.
# Falls back to max(ALLOWED_USER_VERSIONS) for legacy tuple/list form.
# Ported verbatim from update.sh extract_ver_sdk_bridge_py (DGN-364 2.7b).
# Prints the integer on stdout; exits non-zero on parse failure.
_kit_extract_ver_sdk_bridge_py() {
  local f="$1"
  [ -f "$f" ] || return 1
  "$PACK_PY3" -c "
import re, sys, ast
txt = open(sys.argv[1]).read()
m = re.search(r'^MIN_USER_VERSION\s*=\s*([0-9]+)', txt, re.MULTILINE)
if m:
    print(int(m.group(1)))
    sys.exit(0)
m = re.search(r'^ALLOWED_USER_VERSIONS\s*=\s*(\([^)]*\)|\[[^\]]*\])',
              txt, re.MULTILINE)
if not m: sys.exit(1)
vals = ast.literal_eval(m.group(1))
if not vals: sys.exit(1)
print(max(int(x) for x in vals))
" "$f" 2>/dev/null
}

# _kit_resolve_link: defined ABOVE the requires_kit gate (DGN-1461 moved it
# -- the gate's scope check resolves crew DB symlink chains and executes
# long before this point in the script's top-down flow).

# ---- kit-migrate crew lockstep guard (DGN-1436 뇌관) -----------------------
# The kit DB is not instance property -- it is a crew-shared file (worklog
# DGN-1436 measured: <main-agent>/database/kit_life.db -> ~/.dogany/crews/kit_life/
# kit_life.db symlink; ONE file, so PRAGMA user_version is physically
# crew-scoped). The forward-migration loop below only looks at ITS OWN
# code's EXPECTED_USER_VERSION and advances that one file unconditionally --
# zero crew awareness. Advance it while a crew member still runs code pinned
# to a different version and that member's runtime reads a schema it does
# not expect (harmless today only because every member happens to share the
# same pin; the next pack_version that touches schema detonates it).
#
# Crew discovery: no new registry. Reuse the SAME HOME-relative estate scan
# update_plan.sh/update_apply.sh already trust (lib/discover_instances.sh,
# DGN-1037) to get candidate instance roots, then keep only the ones whose
# database/$KIT_NAME.db symlink resolves to the SAME physical file as this
# instance's -- the identical ground-truth check update_apply.sh:69 lists as
# its readlink cross-validation source. crew.conf is deliberately NOT read
# here: that registry is seeded/consumed by the update_apply.sh loop, and
# this guard exists precisely for the path that bypasses that loop (direct
# pack_install.sh invocation) -- crew.conf's existence/freshness cannot be
# assumed here.
#
# Verdict: every crew member's database/$KIT_NAME.py EXPECTED_USER_VERSION
# must equal the pin this run is about to stamp on the shared DB (the same
# exact-match contract kit-migrate enforces on itself a few lines down --
# "payload runtime fail-closes on user_version", not just fails open on a
# newer schema). A member whose pin cannot be read (file missing/unparseable)
# is refused, not passed through (fail-closed, never silently trusted). Zero
# crew members (standalone instance, or a shared DB nobody else mounts yet)
# is a pass -- unchanged behavior for the non-shared case (invariant).
#
# KNOWN OVER-BLOCK (DGN-1436 premise correction, 2026-09-15) -- NARROWED by
# DGN-1517 (spec v1 under that ticket id, LOCKED conditional on B-1/B-2/
# B-3; the re-check dossier is filed under that ticket id -- cited by TICKET
# rather than by path: DGN-1123's installer gate forbids report-tree paths in
# this file, and a ticket id outlives a filename anyway): because this guard
# reads each member's CURRENT on-disk pin (a fact), a legitimate crew-joint
# update (every member moving 50 -> 51 in one apply turn) used to be refused
# at the first member's migrate even though every member's payload was
# already committed to the same shared DB advance. This comment used to
# point at the documented fix as "activate every member BEFORE any member's
# migrate" (the prepare/migrate/activate split, DGN-1509 SPEC v2 S5..S8) --
# DGN-1517's design measured that path directly (crew_layout.sh's branch2,
# `os.replace(live, gen_file)`, silently overwrites a staged NEW payload
# with the OLD live file when activation runs before migrate) and found it
# INCOMPATIBLE with the landed layout-migrate semantics, not merely risky.
# The fix actually landed here is a SECOND EVIDENCE CLASS instead: a
# sibling's current on-disk pin is still what decides lag by default
# (nothing below relaxes that), but a sibling is NOT counted as lagging when
# BOTH (E1) a crew transaction lease is DIRTY, names THIS SOURCE_TAG as its
# txn, and lists that sibling as a member (crew_lease.sh), AND (E2) that
# sibling's OWN disk already carries a staged generation
# (database/.gen/<SOURCE_TAG>/<kit>.py) pinned to the target version
# (pack_install --prepare, hoisted to run for every member before any
# member's migrate -- update_apply.sh Phase P). E1 is what makes this safe:
# the lease is written only after every member bridge is verified stopped
# (update_apply.sh T0-7), so a sibling this guard accepts under E1+E2 has no
# live runtime that could read the schema it does not expect. See
# the DGN-1517 spec v1 §2.1 for the full argument and §2.4 for the
# direct (non-crew) path, which is UNCHANGED (E1 requires a lease that only
# update_apply.sh's crew transaction writes). S1..S4 of DGN-1509 SPEC v2
# remain landed (this guard + crew_lease.sh); the activate-before-migrate
# variant of S5..S8 is REJECTED (not merely deferred) by DGN-1517 §1.2/§2.5-A
# -- do not revive it without re-reading that rejection.
# _kit_crew_lockstep_e1e2 <sibling_root_phys> <target_pin> -- DGN-1517 §2.2
# evidence class 3a. rc 0 iff BOTH E1 (a DIRTY lease for $KIT_NAME whose
# holder txn equals THIS install's SOURCE_TAG and whose members list names
# this sibling) and E2 (the sibling's own staged generation
# database/.gen/$SOURCE_TAG/$KIT_NAME.py already carries EXPECTED_USER_VERSION
# == target_pin) hold. Requires $CREW_DIR and crew_lease.sh's read helpers
# already sourced by the caller (no parsing duplicated here, DGN-1517 §2.2).
# [B-1] SOURCE_TAG empty is checked FIRST and unconditionally refuses --
# empty==empty would otherwise be vacuously true against an equally-empty
# holder txn (pack_install.sh:247 default), and this function is also the
# gate 3b borrows its SOURCE_TAG non-empty requirement from (see caller).
_kit_crew_lockstep_e1e2() {
  local root="$1" target="$2" staged_py staged_pin
  [[ -n "$SOURCE_TAG" ]] || return 1
  [[ "$(_crew_lease_state "$KIT_NAME")" == "DIRTY" ]] || return 1
  [[ "$(_crew_lease_holder_txn "$KIT_NAME")" == "$SOURCE_TAG" ]] || return 1
  _crew_lease_has_member "$KIT_NAME" "$root" || return 1
  staged_py="$root/database/.gen/$SOURCE_TAG/$KIT_NAME.py"
  staged_pin="$(_kit_extract_ver_kit_py "$staged_py" 2>/dev/null || true)"
  [[ "$staged_pin" =~ ^[0-9]+$ ]] || return 1
  [[ "$staged_pin" -eq "$target" ]] || return 1
  return 0
}

# ---- DGN-1517 F-1: E1's "stopped" implication is verified, not inferred ----
# The lease is a LATCH, not a snapshot: bridges are stopped with `launchctl
# bootout` only, the RunAtLoad=true plist copy stays in ~/Library/LaunchAgents
# (mint_run.sh start: `cp -f "$p" "$LA_DIR/"`; _launchd_stop's own header:
# "the unit is held down by the bootout alone"), so a reboot/re-login
# resurrects every member bridge on OLD code while the lease -- which never
# expires (only verified --clear-incomplete recovery releases it) -- still reads
# DIRTY and still names this deterministic txn tag. Nothing in the boot path
# reads the lease. So E1+E2 alone can be TRUE while the sibling's runner is
# LIVE -- exactly the corruption this guard exists to prevent. The two checks
# below disarm that latch; both run ONLY when 3a's evidence already holds,
# and both refuse via _fail (loud, terminal) -- never a silent fall-through
# to some other accept row.

# _kit_crew_lockstep_launchd_list -- `launchctl list` output, or the file
# named by DOGANY_LAUNCHD_LIST when set (test seam: the read-side
# counterpart of update_apply.sh's DOGANY_LAUNCHD_CAPTURE rehearsal seam).
# The seam cannot flip a verdict toward acceptance on its own: an injected
# EMPTY list merely restores the pre-F-1 behavior, and acceptance still
# requires the on-disk E1+E2 evidence (DGN-1506 "환경변수 단독 무효" holds).
_kit_crew_lockstep_launchd_list() {
  if [[ -n "${DOGANY_LAUNCHD_LIST:-}" ]]; then
    cat "$DOGANY_LAUNCHD_LIST" 2>/dev/null
  else
    launchctl list 2>/dev/null
  fi
}

# _kit_crew_lockstep_sibling_stopped <stage> <name> <root> -- every one of
# the sibling's bridge/routines labels must be ABSENT from launchd (same
# label enumeration + `launchctl list | awk $3` verification idiom as
# update_apply.sh _launchd_stop). A live label = the sibling is NOT stopped
# = 3a's whole safety argument is void for it -> _fail. `launchctl` being
# unavailable/erroring yields empty output = labels read as absent -- same
# failure direction as _launchd_stop's own verification, kept identical on
# purpose (two idioms would drift, update_seed precedent).
_kit_crew_lockstep_sibling_stopped() {
  local stage="$1" name="$2" root="$3" p label
  for p in "$root"/bridge/*.plist "$root"/routines/*.plist; do
    [[ -e "$p" ]] || continue
    label="$(plist_label "$p")" \
      || _fail "kit: $stage: cannot determine launchd Label from $p -- refusing to treat the sibling as stopped"
    if _kit_crew_lockstep_launchd_list \
        | awk -v l="$label" '$3==l {found=1} END {exit !found}'; then
      _fail "kit: $stage: CREW LOCKSTEP GUARD: crew member '$name' ($root) has LIVE launchd unit '$label' -- the DIRTY lease's 'runner is stopped' implication does not hold (a reboot re-arms RunAtLoad bridges regardless of the lease), refusing to advance the shared DB under a running sibling (DGN-1517 F-1; stop the unit or complete the crew transaction via update.sh)"
    fi
  done
  return 0
}

# _kit_crew_lockstep_lease_fresh <stage> <name> -- time-disarm of the latch.
# Refuses ONLY when BOTH hold: holder started_at is older than
# _KIT_CREW_LEASE_FRESH_MAX AND the holder pid is dead. Window = 24h.
# Justification (not an invented number): a crew transaction is one
# synchronous update_apply run (minutes), and the sanctioned recovery lane
# (spec §2.4 scenario S / test R8 -- operator completes a hard-killed
# transaction with a direct --source-tag install) is a same-incident,
# same-day act; 24h bounds both with an order of magnitude to spare, so
# this can never fire inside a legitimate transaction or its recovery. Past
# 24h the evidence has decayed: txn tags are DETERMINISTIC
# (pack/<kit>/v<ver>), so an old lease's "same transaction" claim is really
# just "same target version", and the estate has plausibly crossed a
# reboot/re-login boundary (the exact latch failure above). A dead holder
# alone does NOT refuse -- that IS the hard-kill recovery lane (R8/R10b); a
# stale-but-alive holder does not refuse either (a live holder is still
# driving the transaction). Unreadable started_at/pid count as
# stale/dead respectively (fail-closed).
_KIT_CREW_LEASE_FRESH_MAX=86400
_kit_crew_lockstep_lease_fresh() {
  local stage="$1" name="$2" started pid now age
  started="$(_crew_lease_holder_started_at "$KIT_NAME")"
  pid="$(_crew_lease_holder_pid "$KIT_NAME")"
  now="$(date +%s)"
  if [[ "$started" =~ ^[0-9]+$ ]]; then
    age=$((now - started))
    [[ "$age" -gt "$_KIT_CREW_LEASE_FRESH_MAX" ]] || return 0
  fi
  # started_at stale (or unreadable): only a live holder may still vouch.
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  _fail "kit: $stage: CREW LOCKSTEP GUARD: crew member '$name' would be accepted on a STALE lease -- holder started_at='${started:-unreadable}' is older than ${_KIT_CREW_LEASE_FRESH_MAX}s and holder pid='${pid:-unreadable}' is dead. The 'same transaction' evidence has decayed (txn tags are deterministic, a reboot may have re-armed member bridges since). Re-confirm explicitly: verify every member bridge is stopped, complete forward recovery and run bash $SCRIPT_DIR/update_apply.sh --clear-incomplete $KIT_NAME, then raise an update offer for the owner to pick in /update so a FRESH consent + lease authorize this advance (DGN-1517 F-1)"
}

# _kit_crew_sync_door -- the operator line every pin-based lockstep refusal
# carries (DGN-1436, "who syncs the shared DB schema version"). A shared kit
# DB is advanced by exactly one door: update_apply.sh's crew-atomic
# transaction (whole crew = one unit, consent-gated by crew_consent.sh).
# Naming it here is the forcing point: without it the refusal invites the
# per-member hand-sync loop, which can never converge -- each member's
# direct upgrade is refused by this same guard while any other member lags.
_kit_crew_sync_door() {
  printf '%s' "-- a shared kit DB is advanced only by the crew-atomic transaction (all members as one unit); a per-member direct pack_install.sh upgrade cannot converge (each member's run is refused by this guard while any other member lags). Sync the crew: bash $SCRIPT_DIR/update_apply.sh --dry-run; then raise an update offer for kit $KIT_NAME and ask the owner to pick it in /update (the ledger records crew consent) (DGN-1436)"
}

_kit_crew_lockstep_guard() {
  local target_pin="$1"
  # $2 = the DB file whose crew is being checked. Defaults to $_KIT_DB (the
  # migrate call site, where db-init has already created/resolved the
  # instance link). _kit_crew_lockstep_preflight below passes it explicitly
  # because it runs BEFORE db-init, so $_KIT_DB does not exist yet and a
  # first-time joiner has no database/<kit>.db to name (DGN-1436 item 3).
  local db_path="${2:-$_KIT_DB}"
  # $3 = which stage is asking, for the message prefix only. The verdict is
  # identical either way; naming the stage is what lets an operator tell a
  # "nothing was touched" preflight refusal from a migrate-time one.
  local stage="${3:-migrate}"
  local self_real self_root_phys disc_lib lease_lib scan line name root other_db phys other_py other_pin
  local self_cur_pin staged_pin
  local mem_count=0 lag_count=0

  self_real="$(_kit_resolve_link "$db_path")" || return 0

  disc_lib="$SCRIPT_DIR/lib/discover_instances.sh"
  [[ -f "$disc_lib" && -r "$disc_lib" ]] \
    || _fail "kit: $stage: crew-lockstep: instance discovery library missing ($disc_lib) -- fail-closed (DGN-1436)"
  # shellcheck source=lib/discover_instances.sh
  . "$disc_lib"
  scan="$(_discover_instances)" \
    || _fail "kit: $stage: crew-lockstep: HOME estate scan failed (\$HOME unusable) -- cannot rule out a lagging crew member, fail-closed (DGN-1436)"

  # DGN-1517 §2.2: lease read helpers for evidence class E1/E2 (_kit_crew_
  # lockstep_e1e2 above). Sourced here, not at top-level, matching disc_lib's
  # own lazy-source convention -- zero cost for the (majority) non-crew
  # install path that never calls this guard.
  lease_lib="$SCRIPT_DIR/lib/crew_lease.sh"
  [[ -f "$lease_lib" && -r "$lease_lib" ]] \
    || _fail "kit: $stage: crew-lockstep: crew lease library missing ($lease_lib) -- fail-closed (DGN-1517)"
  # shellcheck source=lib/crew_lease.sh
  . "$lease_lib"

  self_root_phys="$(cd -P "$ROOT" 2>/dev/null && pwd)" || self_root_phys="$ROOT"
  # Own current on-disk pin (pre-mutation at preflight time -- kit_core has
  # not copied yet when preflight calls this). Only consulted by 3b below,
  # but cheap to read once regardless of DRY.
  self_cur_pin="$(_kit_extract_ver_kit_py "$ROOT/database/$KIT_NAME.py" 2>/dev/null || true)"

  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    name="${line%%:*}"
    root="${line#*:}"
    [[ "$root" == "$self_root_phys" ]] && continue
    other_db="$root/database/$KIT_NAME.db"
    [[ -e "$other_db" || -L "$other_db" ]] || continue
    phys="$(_kit_resolve_link "$other_db")" || continue
    [[ "$phys" == "$self_real" ]] || continue
    mem_count=$((mem_count + 1))
    other_py="$root/database/$KIT_NAME.py"
    other_pin="$(_kit_extract_ver_kit_py "$other_py" 2>/dev/null || true)"
    if [[ ! "$other_pin" =~ ^[0-9]+$ ]]; then
      _fail "kit: $stage: CREW LOCKSTEP GUARD: crew member '$name' ($root) EXPECTED_USER_VERSION unreadable (database/$KIT_NAME.py missing/unparseable) -- cannot prove it can carry pin=$target_pin on shared $KIT_NAME.db, refusing to advance (fail-closed, DGN-1436) $(_kit_crew_sync_door)"
    fi
    if [[ "$other_pin" -ne "$target_pin" ]]; then
      # DGN-1517 §2.2 row 3a: a lagging on-disk pin is NOT lag when the
      # sibling's own disk also proves (E1 lease + E2 staged generation) it
      # is mid-flight in the SAME transaction this install is carrying.
      if _kit_crew_lockstep_e1e2 "$root" "$target_pin"; then
        # DGN-1517 F-1: evidence held -- now verify the implication the
        # evidence stands on. Both helpers _fail (terminal) on violation;
        # control past these two lines means the sibling is verified
        # stopped AND the lease latch is not time-decayed.
        _kit_crew_lockstep_sibling_stopped "$stage" "$name" "$root"
        _kit_crew_lockstep_lease_fresh "$stage" "$name"
        staged_pin="$(_kit_extract_ver_kit_py "$root/database/.gen/$SOURCE_TAG/$KIT_NAME.py" 2>/dev/null || true)"
        _log "kit: $stage: CREW LOCKSTEP GUARD: crew member '$name' ($root) accepted (STAGED-CURRENT, not counted as lag) -- on-disk pin=$other_pin, staged pin=$staged_pin at database/.gen/$SOURCE_TAG/$KIT_NAME.py (target=$target_pin), lease txn='$SOURCE_TAG' DIRTY names this member (DGN-1517 §2.2 3a)"
        continue
      fi
      # row 3b [DRY=1 only]: T0-5's dry-run preflight runs before the lease
      # exists (update_apply.sh T0-5 precedes 1669) and before Phase P
      # stages anything, so 3a is structurally unreachable there -- this is
      # its preview counterpart. [B-1] SOURCE_TAG non-empty is required
      # explicitly (not inherited from the equality check alone) so an
      # untagged direct dry-run can never satisfy an equally-empty preview.
      if [[ "$DRY" -eq 1 && -n "$SOURCE_TAG" \
            && "${DOGANY_CREW_TXN_PREVIEW:-}" == "$SOURCE_TAG" \
            && -n "$other_pin" && "$other_pin" == "$self_cur_pin" ]]; then
        _log "kit: $stage: CREW LOCKSTEP GUARD: crew member '$name' ($root) accepted (PREVIEW-JOINT, not counted as lag) -- preview txn='$DOGANY_CREW_TXN_PREVIEW' matches this dry-run's source tag and pin=$other_pin matches this instance's own current pin; the real run is re-verified against lease + staged evidence, not this preview (DGN-1517 §2.2 3b)"
        continue
      fi
      lag_count=$((lag_count + 1))
      _log "kit: $stage: CREW LOCKSTEP GUARD: crew member '$name' ($root) pin=$other_pin != required=$target_pin"
    fi
  done <<< "$scan"

  if [[ "$lag_count" -gt 0 ]]; then
    _gate_stop kit.crew-lockstep "kit: $stage: CREW LOCKSTEP GUARD: refusing to advance shared $KIT_NAME.db to pin=$target_pin -- $lag_count of $mem_count crew member(s) sharing this DB are not at that pin (named above; DGN-1436 -- a lagging member's runtime would read a schema it does not expect) $(_kit_crew_sync_door)"
  fi
  if [[ "$mem_count" -gt 0 ]]; then
    _log "kit: $stage: crew-lockstep: $mem_count crew member(s) share $KIT_NAME.db, all at pin=$target_pin -- OK to advance"
  fi
}

# ---- crew lockstep PREFLIGHT: the DIRECT pack_install.sh path (DGN-1436 #3) --
# The guard above is correct but it fires LATE: kit-migrate runs only after
# the kit_core step has already overwritten this instance's
# database/$KIT_NAME.py with the payload's. On the canonical path that is
# harmless -- update_apply.sh runs a MANDATORY per-member
# `pack_install --dry-run` preflight and refuses to enter the crew
# transaction when it fails (update_apply.sh "BLOCKED -- pack_install
# --dry-run 플랜 FAIL"), so the real run never starts against a lagging crew.
# A DIRECT `pack_install.sh --upgrade` invocation has no such preflight. It
# runs all the way to migrate, the guard refuses, and the instance is left
# holding the NEW code pin against the OLD shared DB -- the guard that exists
# to protect the crew leaves the refusing member's own runtime fail-closed on
# user_version. Measured before this preflight existed (crewkit fixture,
# peer pin=2, payload pin=3): rc=1 and the shared DB correctly still at 2,
# but InstA/database/crewkit.py stamped EXPECTED_USER_VERSION = 3.
#
# Fix is altitude, not a new gate: run the SAME guard with the SAME verdict
# at the point where nothing has been written yet. This is exactly the check
# the --dry-run branch already had to perform (dry-run must not mutate); it
# is hoisted to a single call site shared by both modes, so the real path
# inherits the pre-mutation refusal instead of duplicating the logic. The
# migrate-time call is KEPT, not superseded: preflight refuses before this
# run mutates anything, the migrate-time call is the check at the write
# moment itself and reads the pin from the file kit_core actually landed.
#
# Target-pin source is unchanged from the dry-run version, so this can never
# compute a different pin than the migrate step will: the payload .py, or the
# instance .py when the reverse-drift guard decided to keep it (KIT_CORE_SKIP).
# NOTE on that second branch: DGN-1423's admission gate ("a partial skip
# cannot count as a successful installation") _fail's on KIT_CORE_SKIP=1
# before control ever reaches here, so the branch is currently unreachable.
# It is kept rather than deleted to stay verdict-identical with the
# KIT_CORE_SKIP branch kit-migrate still carries -- delete both or neither.
#
# DB to inspect: the instance link when it exists, otherwise the crew
# canonical that _kit_crew_db_link_ensure is about to link this instance to.
# A first-time joiner has no database/$KIT_NAME.db yet, and that is precisely
# the shape where the install would join an existing crew DB and then advance
# a file other members read.
_kit_crew_lockstep_preflight() {
  local target_py target_pin db cur
  [[ "$PACK_CLASS" == "kit" ]] || return 0
  [[ "$KIT_DB_LANE" -eq 1 ]] || return 0
  target_py="$PAYLOAD_KIT_PY"
  if [[ "$KIT_CORE_SKIP" -eq 1 ]]; then
    target_py="$INST_KIT_PY"
  fi
  target_pin="$(_kit_extract_ver_kit_py "$target_py" 2>/dev/null || true)"
  [[ "$target_pin" =~ ^[0-9]+$ ]] || return 0

  db="$ROOT/database/$KIT_NAME.db"
  if [[ ! -e "$db" && ! -L "$db" ]]; then
    db="$SHARED_HOME/crews/$KIT_NAME/$KIT_NAME.db"
    [[ -e "$db" ]] || return 0
  fi

  cur="$(sqlite3 "$db" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
  cur="${cur:-0}"
  [[ "$cur" =~ ^[0-9]+$ ]] || return 0
  [[ "$cur" -lt "$target_pin" ]] || return 0

  # Would advance a possibly-shared DB -> same verdict as migrate, earlier.
  _kit_crew_lockstep_guard "$target_pin" "$db" "preflight"
  return 0
}

# ---- kit crew DB symlink: framework-owned creation + validation (DGN-1436) --
# A kit's DB is crew property, not instance property (worklog DGN-1436):
# instances that install the SAME kit are, by definition, that kit's crew,
# and share ONE physical database file. This mirrors the shared-skills
# convention already in force a few steps below ($SHARED_ROOT =
# $SHARED_HOME/crews/$KIT_NAME/shared-skills, DGN-956) -- the DB gets the
# sibling slot $SHARED_HOME/crews/$KIT_NAME/$KIT_NAME.db. Crew membership is
# NOT a new registry: it falls straight out of provides_kit (same source
# _kit_crew_lockstep_guard above already trusts), so any instance that
# installs this kit automatically joins its crew -- no separate opt-in.
# Until now nothing in the repo created this link; it existed only as a
# hand-made fact (the main agent's kit_life.db link, the exact shape this ticket's
# worklog measured). This function is the sole framework owner of
# database/$KIT_NAME.db's crew-link lifecycle:
#   - absent                  -> CREATE. First crew member materializes the
#                                shared canonical from schema.sql (same
#                                atomic tmp+mv the old instance-local
#                                db-init used); every later member just
#                                symlinks to what's already there. This is
#                                the "first crew member" case (DGN-1436
#                                worklog task list item 2): the alternative
#                                (refuse until a human pre-creates the
#                                shared file) would make EVERY kit
#                                unusable on its very first install, so
#                                create is the only workable verdict.
#   - symlink, correct target -> no-op (idempotent; constraint: an already-
#                                correct link must produce zero change).
#   - symlink, wrong/dangling -> FATAL, names actual vs expected target
#                                (DGN-1436 item 2: a broken or misdirected
#                                link must never pass silently). Before
#                                this function existed, a dangling symlink
#                                here fell through the old -f-based ladder
#                                into the create branch and `mv` SILENTLY
#                                replaced the broken link with a fresh
#                                unrelated real file -- exactly the
#                                silent-corruption failure mode DGN-1436
#                                flags; explicit -L handling closes it.
#   - real file/dir present   -> NEVER touched or converted (constraint:
#                                overwriting instance data to install a
#                                symlink is out of scope and forbidden).
#                                Named in the log so this is never a
#                                silent pass, but NOT fatal: every
#                                instance that minted this kit through the
#                                OLD (pre-DGN-1436) db-init code has
#                                exactly this shape on disk already, and
#                                making that fatal on the next reinstall
#                                would brick every standalone instance in
#                                the estate that predates this change.
# Never called under --dry-run: the DRY branch above `exit 0`s before this
# point is ever reached, so dry-run is a hard no-op for this function too
# (invariant, not a separate code path to keep in sync).
_kit_crew_db_link_ensure() {
  local crew_dir="$SHARED_HOME/crews/$KIT_NAME"
  local crew_db="$crew_dir/$KIT_NAME.db"
  local self_phys crew_db_phys tmp

  if [[ -L "$_KIT_DB" ]]; then
    self_phys="$(_kit_resolve_link "$_KIT_DB")" \
      || _fail "kit: db-init: crew-link: database/$KIT_NAME.db is a DANGLING symlink (target directory unreachable) -- refusing to advance; fix or remove the link manually before reinstalling (DGN-1436)"
    mkdir -p "$crew_dir"
    crew_db_phys="$(cd -P "$crew_dir" 2>/dev/null && pwd)/$KIT_NAME.db"
    if [[ "$self_phys" == "$crew_db_phys" ]]; then
      _log "kit: db-init: database/$KIT_NAME.db already links to the crew-shared DB ($crew_db) -- keep (idempotent)"
      return 0
    fi
    _fail "kit: db-init: crew-link: database/$KIT_NAME.db points to '$self_phys', expected the crew-shared '$crew_db_phys' -- refusing to touch a symlink aimed somewhere else (DGN-1436 wrong-target guard)"
  fi

  if [[ -e "$_KIT_DB" ]]; then
    _log "kit: db-init: database/$KIT_NAME.db is a REAL file/dir, not a crew symlink -- leaving it untouched (framework never overwrites instance data; pre-DGN-1436 standalone shape, DGN-1436)"
    return 0
  fi

  mkdir -p "$crew_dir"
  if [[ -e "$crew_db" ]]; then
    _log "kit: db-init: crew-shared $KIT_NAME.db already exists at $crew_db -- linking (not the first crew member)"
  elif [[ ! -f "$_KIT_SCHEMA" ]]; then
    _log "kit: db-init: no schema.sql at instance database/ -- skipping crew-link (payload shipped none)"
    return 0
  else
    command -v sqlite3 >/dev/null 2>&1 \
      || _fail "kit: db-init: sqlite3 not found -- cannot initialize crew-shared $KIT_NAME.db (DGN-674 F7 hard fail, DGN-1436)"
    tmp="$crew_db.init-tmp.$$"
    rm -f "$tmp"
    if sqlite3 "$tmp" < "$_KIT_SCHEMA"; then
      mv "$tmp" "$crew_db"
      _log "kit: db-init: initialized crew-shared $KIT_NAME.db from schema.sql (first crew member, DGN-1436)"
    else
      rm -f "$tmp"
      _fail "kit: db-init: schema.sql failed to load -- partial crew DB discarded, $crew_db NOT created"
    fi
  fi
  ln -s "$crew_db" "$_KIT_DB" \
    || _fail "kit: db-init: failed to create crew symlink database/$KIT_NAME.db -> $crew_db"
  _log "kit: db-init: database/$KIT_NAME.db -> $crew_db (crew-shared, DGN-1436)"
}

# _kit_conf_merge <instance_conf> <fragment_conf>
# Merge-key: append keys from fragment_conf that are NOT already present in
# instance_conf. Existing keys are never modified (preserves hand-edited values).
# Both files use KEY=VALUE format (lines starting with # are comments, skipped).
# DGN-1526 R3: the keys it did NOT write are reported too, not only the added
# ones. A skipped key whose instance value differs from the payload value is
# the local layer winning over a shipped default -- named as 'differs' so the
# owner (and the log reader) can see which shipped change did not land.
# Values are never printed (a conf line may carry a secret). One line per
# verdict; callers log every line.
_kit_conf_merge() {
  local inst="$1" frag="$2"
  "$PACK_PY3" - "$inst" "$frag" <<'PYEOF'
import sys, re, pathlib

inst_path = pathlib.Path(sys.argv[1])
frag_path = pathlib.Path(sys.argv[2])

KEY_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$')

# Parse existing keys from instance config (KEY=... -> first value seen).
existing = {}
inst_lines = inst_path.read_text(encoding="utf-8").splitlines() if inst_path.exists() else []
for line in inst_lines:
    line = line.rstrip("\r")
    if line.startswith("#") or not line.strip():
        continue
    m = KEY_RE.match(line)
    if m and m.group(1) not in existing:
        existing[m.group(1)] = m.group(2).strip()

# Append fragment keys not already present (merge-key: add-only, no clobber).
added = []
added_keys = set()
skipped = []  # (key, 'differs' | 'same') -- instance value kept
frag_text = frag_path.read_text(encoding="utf-8") if frag_path.exists() else ""
for line in frag_text.splitlines():
    line_stripped = line.rstrip("\r")
    if line_stripped.startswith("#") or not line_stripped.strip():
        continue
    m = KEY_RE.match(line_stripped)
    if m:
        key = m.group(1)
        if key in added_keys:
            continue  # duplicate inside the fragment itself: first one won
        if key in existing:
            same = existing[key] == m.group(2).strip()
            skipped.append((key, "same" if same else "differs"))
            continue
        added.append(line_stripped)
        added_keys.add(key)

if added:
    # Append new keys, ensuring trailing newline before appending.
    inst_path.parent.mkdir(parents=True, exist_ok=True)
    current = inst_path.read_text(encoding="utf-8") if inst_path.exists() else ""
    if current and not current.endswith("\n"):
        current += "\n"
    current += "\n".join(added) + "\n"
    inst_path.write_text(current, encoding="utf-8")
    print("[kit-conf-merge] added keys: " + ", ".join(added))
else:
    print("[kit-conf-merge] no new keys to add (all already present)")
differs = [k for k, v in skipped if v == "differs"]
same = [k for k, v in skipped if v == "same"]
if differs:
    print("[kit-conf-merge] skipped keys, instance value kept (differs from payload -- shipped value NOT applied): "
          + ", ".join(differs))
if same:
    print("[kit-conf-merge] skipped keys, already present with the payload value: " + ", ".join(same))
PYEOF
}

# DGN-1789: the kit i18n override layer writer is scripts/pack/lib/
# kit_i18n_layer.py (one writer; update.sh's old-model migration calls the
# same file). See the i18n-layer step below.
KIT_I18N_LAYER_LIB="${DOGANY_KIT_I18N_LAYER_LIB:-$SCRIPT_DIR/lib/kit_i18n_layer.py}"
# DGN-1500: the term dictionary layer writer (payload/config/terms.json ->
# config/packs/<pack-id>.terms.json). See the term-dict-layer step below.
TERM_DICT_LAYER_LIB="${DOGANY_TERM_DICT_LAYER_LIB:-$SCRIPT_DIR/lib/term_dict_layer.py}"

# ---------- kit skills sharing_mode (DGN-956) --------------------------------
# Optional top-level manifest block (additive; contract_version stays 1,
# DGN-783 B4 precedent):
#   "skills": [ {"name": "<payload skills-bundle dir>",
#                "sharing_mode": "share"|"own"} ]
# DEFAULT = own: absent block or unlisted skill keeps today's per-instance
# copy loop byte-identical (back-compat invariant, fixture S6).
# Invalid modes / dead declarations are refused before install by compat-lint
# C7 (fail-closed) -- here the installer only distinguishes 'share' from
# everything else.
KIT_SKILL_MODE_LINES="$("$PACK_PY3" - "$MANIFEST" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
for e in d.get("skills") or []:
    if isinstance(e, dict) and e.get("name"):
        print("%s\t%s" % (e["name"], e.get("sharing_mode") or ""))
PYEOF
)"

_kit_skill_mode() { # _kit_skill_mode <skill-name> -- prints 'share' or 'own'
  local m
  m="$(printf '%s\n' "$KIT_SKILL_MODE_LINES" | awk -F'\t' -v n="$1" '$1==n{print $2; exit}')"
  if [[ "$m" == "share" ]]; then echo share; else echo own; fi
}

# _kit_semver_cmp <a> <b> -- prints -1 / 0 / 1 (a<b / a==b / a>b);
# prints 'x' when either side does not parse as x.y.z.
_kit_semver_cmp() {
  "$PACK_PY3" -c "
import sys
def p(s):
    try:
        return tuple(int(x) for x in s.strip().split('.')[:3])
    except Exception:
        return None
a, b = p(sys.argv[1]), p(sys.argv[2])
print('x' if a is None or b is None else (a > b) - (a < b))
" "$1" "$2" 2>/dev/null || echo "x"
}

# _kit_share_skill <payload_skill_dir> <skill_name>
# DGN-956 share branch, two phases:
#   (1) materialize/refresh the crew-shared canonical at $SHARED_ROOT/<skill>
#       (the installer is the SOLE WRITER of that root). Staging + atomic mv
#       swap minimizes the window in which another instance's symlink sees a
#       half-copied body. Version anchor = <skill>/.pack-stamp; downgrades are
#       refused loudly (reverse-drift guard philosophy, DGN-803 LS-4/LS-5).
#   (2) point the instance link site .claude/skills-bundle/<skill> at the
#       shared canonical by symlink. Mint path: a newly minted instance
#       receives the skill BY REFERENCE (no divergent copy). A pre-existing
#       REAL dir converts only when byte-identical; a DIVERGED real dir is a
#       fail-closed FATAL (F4 -- hand-enhanced instance skills are
#       upstream-candidate assets, never clobbered).
# Idempotent: equal pack_version + correct link = full no-op.
_kit_share_skill() {
  local src="$1" name="$2"
  local shared="$SHARED_ROOT/$name"
  local stamp="$shared/.pack-stamp"
  local site="$ROOT/.claude/skills-bundle/$name"
  local ts; ts="$(date +%Y%m%d-%H%M%S)"

  mkdir -p "$SHARED_ROOT"

  # ---- (1) shared canonical materialize/refresh ----
  local stamp_ver="" action=""
  if [[ -f "$stamp" ]]; then
    stamp_ver="$(sed -n 's/^pack_version=//p' "$stamp" | head -1)"
  fi
  if [[ -z "$stamp_ver" ]]; then
    action="materialize"
  else
    local cmp; cmp="$(_kit_semver_cmp "$PACK_VERSION" "$stamp_ver")"
    case "$cmp" in
      1)  action="refresh" ;;
      0)  action="noop" ;;
      -1) action="skip-downgrade" ;;
      *)  action="skip-unparseable" ;;
    esac
  fi

  case "$action" in
    materialize|refresh)
      local stage="$SHARED_ROOT/.stage.$name.$$"
      rm -rf "$stage"
      cp -R "$src" "$stage"
      find "$stage" -name '.gitkeep' -delete
      {
        printf 'pack_id=%s\n' "$PACK_ID"
        printf 'pack_version=%s\n' "$PACK_VERSION"
        printf 'installed_at=%s\n' "$ts"
      } > "$stage/.pack-stamp"
      if [[ -e "$shared" || -L "$shared" ]]; then
        # Preserve the previous shared body (upgrade path, or a stampless
        # pre-existing dir e.g. hand-landed) -- never delete data.
        mv "$shared" "$shared.prev-$ts"
        _log "  skills-bundle: $name (share): previous shared body preserved -> $name.prev-$ts"
        # DGN-1395: the writer prunes its own backups, right where it made
        # one. Retention = the single predicate in lib/prev_backup_prune.sh
        # (newest PREV_BACKUP_KEEP_N, OR younger than PREV_BACKUP_KEEP_DAYS);
        # the backup just created is part of the set being ranked, and $ts is
        # the reference clock so one install run judges every age by the same
        # instant. Never fails the install.
        local pruned
        while IFS= read -r pruned; do
          [[ -n "$pruned" ]] || continue
          _log "  skills-bundle: $name (share): $pruned"
        done < <(prev_backup_prune "$SHARED_ROOT" "$name" "$ts")
      fi
      mv "$stage" "$shared"
      _log "  skills-bundle: $name (share): shared canonical ${action}d at $shared (pack_version=$PACK_VERSION)"
      ;;
    noop)
      _log "  skills-bundle: $name (share): shared canonical up to date (pack_version=$PACK_VERSION) -- no-op"
      ;;
    skip-downgrade)
      _log "  skills-bundle: $name (share): SKIP -- payload pack_version=$PACK_VERSION < shared stamp=$stamp_ver (never downgrade)"
      ;;
    skip-unparseable)
      _log "  skills-bundle: $name (share): SKIP refresh -- cannot compare pack_version='$PACK_VERSION' vs stamp='$stamp_ver' (fail-safe: shared body untouched)"
      ;;
  esac

  # ---- (2) instance link site ----
  # NEVER cp through an existing symlink (-L pre-check is mandatory: cp -f
  # write-through would overwrite the SHARED body with instance-view bytes).
  if [[ -L "$site" ]]; then
    local cur; cur="$(readlink "$site")"
    if [[ "$cur" == "$shared" ]]; then
      _log "  skills-bundle: $name (share): link OK -> $shared (no-op)"
    else
      rm "$site"
      ln -s "$shared" "$site"
      _log "  skills-bundle: $name (share): RELINKED $site ($cur -> $shared)"
    fi
  elif [[ -d "$site" ]]; then
    [[ -d "$shared" ]] || _fail "kit: skills-bundle: $name is 'share' but the shared canonical is missing ($shared) while a real instance dir exists at $site -- refusing to guess"
    if diff -r -x '.pack-stamp' "$site" "$shared" >/dev/null 2>&1; then
      # Lossless conversion of an existing copy-mint instance.
      mv "$site" "$site.pre-share-$ts"
      ln -s "$shared" "$site"
      _log "  skills-bundle: $name (share): byte-identical real dir converted to link (old copy -> $name.pre-share-$ts)"
    else
      # F4 fail-closed: never clobber a diverged real dir.
      _fail "kit: skills-bundle: $name is 'share' but $site is a REAL DIVERGED directory -- fail-closed (F4). Resolve manually: upstream the local delta into the pack canonical, or classify '$name' as sharing_mode=own in pack-manifest.json"
    fi
  elif [[ -e "$site" ]]; then
    _fail "kit: skills-bundle: $name is 'share' but $site exists and is neither a symlink nor a directory -- fail-closed"
  else
    ln -s "$shared" "$site"
    _log "  skills-bundle: $name (share): linked $site -> $shared (mint path: reference, not a copy)"
  fi
}

# _subst_mint_tokens <file> -- substitute the mint identity tokens in place.
# Substitutes exactly:
#   __(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|AGENT_PREFIX|HOME|AGENT_LANG)__
# Values sourced from <root>/.instance.conf (DOGANY_* fields) plus
# config/agent.conf AGENT_LANG. Mirrors update.sh subst_one (~L1053-1066):
# minimal sed, no other tokens; identity tokens are substituted only when the
# instance identity is complete (never write empty labels -- residue is caught
# by the G4 unrendered-token gate instead).
# CROSS-REF: the token list appears in four places that must stay in sync:
#   (1) mint.sh sanity check (~L504 alternation)
#   (2) update.sh subst_one (~L1053-1066)
#   (3) pack_install.sh _subst_mint_tokens (this function)
#   (4) G4 unrendered-token check (scripts/pack/knowledge_selftest.sh)
# When adding a token, update all four sites and their cross-ref comments.
# Defined ahead of the pack-class dispatch below (DGN-1402): the contract-class
# kit routines-schedules step needs it too, not just the legacy STEP 7b calls
# further down -- same function, same behavior, moved up so both call sites
# can reach it.
_subst_mint_tokens() {
  local f="$1"
  local agent_name="" agent_label="" user_label="" agent_prefix="" agent_lang=""
  if [[ -f "$ROOT/.instance.conf" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "$ROOT/.instance.conf"
    agent_name="${DOGANY_AGENT_NAME:-}"
    agent_label="${DOGANY_AGENT_LABEL:-}"
    user_label="${DOGANY_USER_LABEL:-}"
    # DGN-828 read-side: same resolver as update.sh (instance's own
    # routines/lib/agent_prefix.py -- manifest field scrubbed of placeholder
    # sentinels, else persona '- Emoji:', else empty = render no prefix).
    # A placeholder sentinel is never substituted into pack files.
    agent_prefix="$("$PACK_PY3" "$ROOT/routines/lib/agent_prefix.py" "$ROOT" 2>/dev/null || true)"
  fi
  agent_lang="$(grep -E '^AGENT_LANG=' "$ROOT/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2 || true)"
  agent_lang="${agent_lang:-en}"

  local sed_args=(-e "s#__PROJECT_ROOT__#${ROOT}#g" -e "s#__HOME__#${HOME}#g")
  if [[ -n "$agent_name" && -n "$agent_label" && -n "$user_label" ]]; then
    sed_args+=(-e "s#__AGENT_NAME__#${agent_name}#g" \
               -e "s#__AGENT_LABEL__#${agent_label}#g" \
               -e "s#__USER_LABEL__#${user_label}#g" \
               -e "s#__AGENT_PREFIX__#${agent_prefix}#g" \
               -e "s#__AGENT_LANG__#${agent_lang}#g")
  else
    _log "  WARN: instance identity incomplete (.instance.conf) -- identity token substitution skipped (contract fragments refuse residue via _mint_residue_check; legacy skill-side residue FAILs the knowledge selftest G4)"
  fi
  # DGN-1845: temp sibling + rename (was a $TMPDIR mktemp + mv: a
  # cross-filesystem mv is a copy, and the 0600 temp mode replaced the file's).
  atomic_write_cmd "$f" sed "${sed_args[@]}" "$f" \
    || _fail "mint token substitution: rewrite of $f failed -- install aborted"
}

# _resolve_mint_agent_name -- echo the instance's real agent name (from
# .instance.conf) when identity is complete, else echo nothing (DGN-1402).
# Shared resolver behind the two 'telegram-agent' literal substitutions
# below -- same completeness guard as _subst_mint_tokens (never substitute a
# half-set identity).
_resolve_mint_agent_name() {
  local agent_name="" agent_label="" user_label=""
  if [[ -f "$ROOT/.instance.conf" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "$ROOT/.instance.conf"
    agent_name="${DOGANY_AGENT_NAME:-}"
    agent_label="${DOGANY_AGENT_LABEL:-}"
    user_label="${DOGANY_USER_LABEL:-}"
  fi
  [[ -n "$agent_name" && -n "$agent_label" && -n "$user_label" ]] && printf '%s\n' "$agent_name"
}

# _subst_mint_agent_name_basename <basename> -- render a plist FILENAME
# (DGN-1402). Framework convention for launchd unit basenames is the literal
# string 'telegram-agent' (NOT the __AGENT_NAME__ content token) -- mint.sh
# renders it the same way for agents/.template/routines/plists.defer and
# every plist filename it ships (routines/plists.defer NOTE comment). A
# kit-shipped schedule plist must use the same literal so its rendered
# basename matches the deny-list entries the framework already carries for
# it (agents/.template/routines/plists.defer, DGN-1286 item 2b).
_subst_mint_agent_name_basename() {
  local b="$1" agent_name
  agent_name="$(_resolve_mint_agent_name)"
  if [[ -n "$agent_name" ]]; then
    printf '%s\n' "${b//telegram-agent/$agent_name}"
  else
    _log "  WARN: instance identity incomplete (.instance.conf) -- basename substitution skipped: $b"
    printf '%s\n' "$b"
  fi
}

# _subst_mint_agent_name_content <file> -- same 'telegram-agent' literal
# substitution as _subst_mint_agent_name_basename, applied in place to a
# file's content (DGN-1402; used for a kit-shipped routines/plists.defer
# fragment, whose basenames are plain text lines, not __TOKEN__ markers).
_subst_mint_agent_name_content() {
  local f="$1" agent_name
  agent_name="$(_resolve_mint_agent_name)"
  if [[ -n "$agent_name" ]]; then
    atomic_write_cmd "$f" sed "s/telegram-agent/${agent_name}/g" "$f" \
      || _fail "agent-name substitution: rewrite of $f failed -- install aborted"
  else
    _log "  WARN: instance identity incomplete (.instance.conf) -- content substitution skipped: $f"
  fi
}

# ---------- shared delivery steps (DGN-1467) ---------------------------------
# The agent.conf fragment append (legacy STEP 3), the persona fragment append
# (legacy STEP 7b) and the knowledge wiring selftest (legacy STEP 7c) were
# stranded in the legacy agent/module region BELOW the contract-class
# `exit 0` -- structurally unreachable for contract packs (worklog DGN-1467:
# five separately-reported delivery defects were all exits of that one hole).
# Single logic home, called by BOTH paths -- no re-implementation (duplicate
# delivery logic has a measured drift precedent: the DGN-1018 python `$`
# anchor split even under a verdict-identical contract). The legacy call
# sites keep their position, order and argument meaning (DGN-1018 §5-B
# no-regression); the contract-class path calls the same functions with
# contract inputs (payload fragments rendered via _subst_mint_tokens -- a
# contract pack has no legacy reference identity, so _render_to does not
# apply).
#
# mode=legacy keeps the pre-extraction semantics byte-exact, INCLUDING the
# single-marker detection greps and the marker line inside the appended
# block. mode=contract has no legacy single-marker history: no marker line,
# no single-marker greps.

# Peer-integration keys belong to the MIGRATION path only (DGN-284 #3/#6).
# DGN-227 E2-1/P24: MIGRATION_PEER joins the migration key family (fresh
# strips it). HANDOFF_PEER_MAIN is a BRIEFING-topology key, NOT a migration
# key -- it is deliberately NOT in this strip list (fresh paths may write it).
# Hoisted from legacy STEP 3 (DGN-1467): the contract agent-conf-fragment
# step refuses the same key family (legacy migration vocabulary must not be
# smuggled through a contract fragment).
PEER_KEYS_RE='^(L1_DB|L1_EXPECTED_USER_VERSION|HANDOFF_PEER_AG|MIGRATION_PEER)='

# _frag_block_emit <begin> <end> <body_file> [<legacy_marker>] -- print one
# fragment block (leading blank line, BEGIN, optional legacy marker, body,
# END). Shared by the two fragment appends below (DGN-1845 atomic append).
_frag_block_emit() {
  echo ""
  echo "$1"
  if [[ -n "${4:-}" ]]; then echo "$4"; fi
  cat "$3"
  echo "$2"
}

# _conf_fragment_apply <label> <conf_file> <body_file> <pack_id> <legacy_marker> <mode> <append_log_msg>
# DGN-227 D2/P8: fragments are managed as BEGIN/END marker-pair blocks so
# --upgrade can replace them (remove-and-reappend). Legacy single-marker
# blocks cannot be bounded mechanically -> --upgrade loud-FAILs on them.
_conf_fragment_apply() {
  local label="$1" conf="$2" body="$3" pid="$4" marker="$5" mode="$6" okmsg="$7"
  local pair_begin="# DOGANY-PACK:$pid:BEGIN"
  local pair_end="# DOGANY-PACK:$pid:END"
  _FRAG_OUTCOME=""
  if [[ "$UPGRADE" -eq 1 ]] && grep -qF "$pair_begin" "$conf" 2>/dev/null; then
    # DGN-1467 grill #2: refuse to excise an UNBOUNDED block. A BEGIN whose
    # closing END line was lost (user edit) makes the excise below delete
    # everything from BEGIN to EOF at rc=0 -- user keys silently destroyed.
    # A pair that does not pair cannot be bounded mechanically -> loud-FAIL
    # (DGN-227 D2), never a guess.
    awk -v b="$pair_begin" -v e="$pair_end" '
      $0 == b { inblk=1; closed=0; next }
      $0 == e && inblk { inblk=0; closed=1 }
      END { exit (inblk || !closed) ? 1 : 0 }' "$conf" \
      || _fail "$label: --upgrade found the BEGIN marker but no closing END marker ('$pair_end') after it in $conf -- the block cannot be bounded mechanically; excising would silently delete everything from BEGIN to end-of-file. Restore the END marker line (or remove the whole block by hand), then re-run (loud-FAIL by design, DGN-227 D2)"
    # marker-pair replacement: excise the old block, then fall through to append
    local _tmp_conf
    _tmp_conf="$(mktemp)"
    awk -v b="$pair_begin" -v e="$pair_end" '
      $0 == b { inblk=1; next }
      $0 == e { inblk=0; next }
      !inblk { print }' "$conf" > "$_tmp_conf"
    mv "$_tmp_conf" "$conf"
    _log "  upgrade: excised prior agent.conf fragment block (marker pair)"
  elif [[ "$UPGRADE" -eq 1 && "$mode" == "legacy" ]] && grep -qF "$marker" "$conf" 2>/dev/null; then
    _fail "$label: --upgrade found a LEGACY single-marker fragment block (no BEGIN/END pair) in agent.conf -- cannot bound it mechanically. Manual migration: remove the old block, then re-run (loud-FAIL by design, DGN-227 D2)"
  fi

  if grep -qF "$pair_begin" "$conf" 2>/dev/null \
     || { [[ "$UPGRADE" -eq 0 && "$mode" == "legacy" ]] && grep -qF "$marker" "$conf" 2>/dev/null; }; then
    _FRAG_OUTCOME="already-present"
    _log "  agent.conf.add already appended (idempotent, skipping)"
  else
    # DGN-1467 grill #1: bash 3.2 errexit does NOT trip on a compound
    # command's redirect failure (measured: $?=1 yet the next statement
    # runs), so an unwritable target used to log "$okmsg" and continue --
    # the || guard is mandatory. The grep re-read below confirms the
    # ARTIFACT, not the write call: END is the last byte written, so its
    # presence also rules out a partial (ENOSPC-truncated) landing that a
    # BEGIN-only probe would miss.
    # DGN-1845: atomic append (temp sibling + rename) -- a live reader sees
    # the file without the block or with all of it.
    atomic_append_cmd "$conf" _frag_block_emit "$pair_begin" "$pair_end" "$body" \
      "$([[ "$mode" == "legacy" ]] && printf '%s' "$marker")" \
      || _fail "$label: append to $conf FAILED (write error above) -- fragment NOT delivered (loud-FAIL, DGN-1467)"
    { grep -qF "$pair_begin" "$conf" && grep -qF "$pair_end" "$conf"; } 2>/dev/null \
      || _fail "$label: appended fragment block NOT found in $conf on re-read (marker pair absent/partial) -- fragment NOT delivered (loud-FAIL, DGN-1467)"
    _FRAG_OUTCOME="appended"
    _log "$okmsg"
  fi
}

# _persona_fragment_apply <label> <persona_md> <rendered_body_file> <pack_id> <legacy_marker> <mode> <append_log_msg>
# DGN-227 D2/P8: BEGIN/END marker-pair block (HTML comment pair for md).
_persona_fragment_apply() {
  local label="$1" agent_md="$2" body="$3" pid="$4" marker="$5" mode="$6" okmsg="$7"
  local pair_begin="<!-- DOGANY-PACK:$pid:BEGIN -->"
  local pair_end="<!-- DOGANY-PACK:$pid:END -->"
  _FRAG_OUTCOME=""
  if [[ "$UPGRADE" -eq 1 ]] && grep -qF "$pair_begin" "$agent_md" 2>/dev/null; then
    # DGN-1467 grill #2: same unbounded-block refusal as
    # _conf_fragment_apply -- a lost END line must not turn the excise into
    # a silent BEGIN-to-EOF persona wipe (DGN-227 D2 loud-FAIL).
    awk -v b="$pair_begin" -v e="$pair_end" '
      $0 == b { inblk=1; closed=0; next }
      $0 == e && inblk { inblk=0; closed=1 }
      END { exit (inblk || !closed) ? 1 : 0 }' "$agent_md" \
      || _fail "$label: --upgrade found the BEGIN marker but no closing END marker ('$pair_end') after it in $agent_md -- the block cannot be bounded mechanically; excising would silently delete everything from BEGIN to end-of-file. Restore the END marker line (or remove the whole block by hand), then re-run (loud-FAIL by design, DGN-227 D2)"
    # DEST-ADJACENT mktemp (settings.json/AGENT-OPS.md precedent, update.sh
    # 3g/3k2) so the mv below is a same-filesystem rename. cp -p stamps
    # the persona file's mode onto the 0600 mktemp file BEFORE the awk
    # redirect truncates it (sed_inplace precedent, update.sh:232-247) --
    # otherwise the excise silently regresses the persona file to mode 0600.
    local _tmp_md
    _tmp_md="$(mktemp "$agent_md.excise.XXXXXX")"
    cp -p "$agent_md" "$_tmp_md"
    awk -v b="$pair_begin" -v e="$pair_end" '
      $0 == b { inblk=1; next }
      $0 == e { inblk=0; next }
      !inblk { print }' "$agent_md" > "$_tmp_md"
    mv "$_tmp_md" "$agent_md"
    _log "  upgrade: excised prior AGENT.md fragment block (marker pair)"
  elif [[ "$UPGRADE" -eq 1 && "$mode" == "legacy" ]] && grep -qF "$marker" "$agent_md" 2>/dev/null; then
    _fail "$label: --upgrade found a LEGACY single-marker AGENT.md fragment (no BEGIN/END pair) -- cannot bound it mechanically. Manual migration: remove the old block, then re-run (loud-FAIL by design, DGN-227 D2)"
  fi
  if grep -qF "$pair_begin" "$agent_md" 2>/dev/null \
     || { [[ "$UPGRADE" -eq 0 && "$mode" == "legacy" ]] && grep -qF "$marker" "$agent_md" 2>/dev/null; }; then
    _FRAG_OUTCOME="already-present"
    _log "  AGENT.md fragment already appended (idempotent, skipping)"
  else
    # DGN-1467 grill #1: same write-guard + artifact re-read as
    # _conf_fragment_apply (bash 3.2 errexit misses compound-redirect
    # failure; END presence rules out a partial landing).
    atomic_append_cmd "$agent_md" _frag_block_emit "$pair_begin" "$pair_end" "$body" "" \
      || _fail "$label: append to $agent_md FAILED (write error above) -- fragment NOT delivered (loud-FAIL, DGN-1467)"
    { grep -qF "$pair_begin" "$agent_md" && grep -qF "$pair_end" "$agent_md"; } 2>/dev/null \
      || _fail "$label: appended fragment block NOT found in $agent_md on re-read (marker pair absent/partial) -- fragment NOT delivered (loud-FAIL, DGN-1467)"
    _FRAG_OUTCOME="appended"
    _log "$okmsg"
  fi
}

# _knowledge_selftest_run <label> [extra selftest args...]
# Warehouse packs: gates G1-G4 (delivery / discovery / refraction predicates).
# Warehouse-less packs: inverse check (zero warehouse artifacts). Contract
# packs: contract lane (knowledge_selftest.sh branches on manifest shape,
# DGN-1467). Same script is re-run by the agent-crafting phase 2 checklist
# (single logic home). G5 (live probes) stays manual.
_knowledge_selftest_run() {
  local label="$1"; shift
  local selftest_sh="$SCRIPT_DIR/knowledge_selftest.sh"
  [[ -x "$selftest_sh" ]] || _fail "$label: knowledge_selftest.sh not found/executable: $selftest_sh"
  local selftest_out selftest_rc line
  selftest_out="$(mktemp)"
  set +e
  "$selftest_sh" "$ROOT" --manifest "$MANIFEST" "$@" > "$selftest_out" 2>&1
  selftest_rc=$?
  set -e
  while IFS= read -r line; do _log "  selftest: $line"; done < "$selftest_out"
  rm -f "$selftest_out"
  if [[ "$selftest_rc" -ne 0 ]]; then
    _fail "$label: knowledge wiring selftest FAILED (exit $selftest_rc) -- the wiring gates must pass at install time"
  fi
}

# ---- delivery census (DGN-1467 detection repair, contract-class path) ----
# Every rostered delivery step must leave a verdict (run / already-present /
# skip:<reason> / deferred:<ticket>) before the contract DONE block; the
# census seal on the only road to DONE fail-closes a missing verdict. "A
# path ends before a delivery step" (this ticket's defect shape) can
# therefore never be silent again -- the check lives in the installer body,
# outside model judgment. `already-present` (DGN-1467 grill #3) records an
# idempotent skip DISTINCTLY from a fresh append: "left the old block in
# place" must never read as "delivered this payload's block".
# Census lands at config/packs/<id>.delivery.tsv for machine consumers.
_DELIVERY_ROSTER="agent_conf_fragment persona_fragment hook_wiring knowledge_selftest crew_assets mailbox_provision bot_start minting_state_accept"
_DELIVERY_CENSUS=""
_census_record() { # _census_record <step> <verdict>
  _DELIVERY_CENSUS="${_DELIVERY_CENSUS}$1"$'\t'"$2"$'\n'
  _log "  delivery-census: $1 $2"
}

# ---------- declared hook wiring (DGN-1496) ----------------------------------
# A pack declares its Claude Code hooks in the manifest:
#   "hooks": [ { "event": "SessionStart", "script": "routines/bundle/x.py" } ]
# The installer wires EXACTLY what is declared -- it knows neither filename
# nor hook event. (The old STEP 5 pinned both: routines/ledger-inject.py +
# UserPromptSubmit -- a condition no contract pack could satisfy after
# compat-lint C4 closed routines/ top level, DGN-803; measured DGN-1496.)
# The installer never interprets the script's CONTENTS -- it only wires a
# declaration, keeping the layer boundary intact.
#
# Validation is fail-closed AT DECLARATION, loud, BEFORE any settings.json
# write (a silent skip is the defect this machinery replaces):
#   * event must be in the Claude Code hook-event vocabulary below;
#   * script must be a relative, traversal-free payload path (absolute or
#     '..' REFUSED -- the declaration is attacker-adjacent input);
#   * script must live on the routines surface this install path actually
#     DELIVERS -- contract class: routines/bundle/* (compat-lint C4
#     allowlist lockstep; do NOT widen: routines/ top level is framework
#     core); legacy class: flat routines/*.py|*.sh (what legacy STEP 2
#     copies). A declaration outside that surface fails HERE, naming the
#     path convention -- otherwise C4 blocks the file, the wire step sees
#     'file not found', and the author never learns the real reason;
#   * script must exist in the payload (dead declaration = FAIL);
#   * optional "timeout" must be a positive integer <= 600 (default 10,
#     the value the old hardcoded step used).
_HOOK_EVENTS="PreToolUse PostToolUse UserPromptSubmit Notification Stop SubagentStop PreCompact SessionStart SessionEnd"

_hooks_declared() { # _hooks_declared <manifest> -- 0 when hooks[] is present and non-empty
  [[ "$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); h=d.get('hooks'); print(1 if isinstance(h,list) and len(h)>0 else 0)" "$1" 2>/dev/null || echo 0)" == "1" ]]
}

_hooks_validate() { # _hooks_validate <label> <manifest> <payload_dir> <bundle|flat>
  # Sets _HOOK_LINES to validated "event<TAB>script<TAB>timeout" lines; any
  # violation -> _fail (loud, exit 1, nothing written anywhere). Result is
  # returned via a global, NOT stdout: a $(...) caller would trap _fail in a
  # subshell and swallow the exit.
  local label="$1" manifest="$2" payload="$3" surface="$4"
  local _hv_out="" _hv_rc=0
  _hv_out="$("$PACK_PY3" - "$manifest" "$payload" "$surface" "$_HOOK_EVENTS" <<'PYEOF'
import json, os, sys

manifest, payload, surface, events = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
known = set(events.split())

def die(msg):
    print("HOOKS-INVALID: %s" % msg)
    sys.exit(3)

d = json.load(open(manifest))
hooks = d.get("hooks")
if not isinstance(hooks, list) or not hooks:
    die("manifest 'hooks' must be a non-empty list of {event, script} objects")
rows = []
for i, h in enumerate(hooks):
    if not isinstance(h, dict):
        die("hooks[%d] is not an object" % i)
    unknown = set(h) - {"event", "script", "timeout"}
    if unknown:
        die("hooks[%d] unknown field(s): %s (allowed: event, script, timeout)"
            % (i, ", ".join(sorted(unknown))))
    ev = h.get("event")
    sc = h.get("script")
    if not isinstance(ev, str) or not ev:
        die("hooks[%d] missing string field 'event'" % i)
    if ev not in known:
        die("hooks[%d] event '%s' is not a Claude Code hook event (known: %s)"
            % (i, ev, " ".join(sorted(known))))
    if not isinstance(sc, str) or not sc:
        die("hooks[%d] missing string field 'script'" % i)
    if sc.startswith("/") or sc.startswith("~"):
        die("hooks[%d] script '%s' is absolute -- declare a payload-relative path" % (i, sc))
    parts = sc.split("/")
    if ".." in parts or "." in parts or "" in parts:
        die("hooks[%d] script '%s' contains traversal or empty path segments" % (i, sc))
    if surface == "bundle":
        if not sc.startswith("routines/bundle/"):
            die("hooks[%d] script '%s' violates the pack path convention -- a contract "
                "pack's hook script must live under routines/bundle/ (the compat-lint C4 "
                "payload allowlist surface; routines/ top level is framework core and "
                "will NOT be delivered)" % (i, sc))
    else:
        if not (len(parts) == 2 and parts[0] == "routines"):
            die("hooks[%d] script '%s' is outside the legacy routines surface -- the "
                "legacy package-copy step delivers flat routines/*.sh|*.py only" % (i, sc))
    if not (sc.endswith(".py") or sc.endswith(".sh")):
        die("hooks[%d] script '%s' has no runnable extension (.py or .sh)" % (i, sc))
    if not os.path.isfile(os.path.join(payload, sc)):
        die("hooks[%d] script '%s' not found in the payload (%s) -- dead declaration"
            % (i, sc, os.path.join(payload, sc)))
    to = h.get("timeout", 10)
    if isinstance(to, bool) or not isinstance(to, int) or to < 1 or to > 600:
        die("hooks[%d] timeout %r invalid (positive integer <= 600)" % (i, h.get("timeout")))
    rows.append((ev, sc, to))
for ev, sc, to in rows:
    print("%s\t%s\t%d" % (ev, sc, to))
PYEOF
)" || _hv_rc=$?
  if [[ "$_hv_rc" -ne 0 || "$_hv_out" == HOOKS-INVALID:* || -z "$_hv_out" ]]; then
    _fail "$label: hooks declaration REFUSED -- ${_hv_out#HOOKS-INVALID: }"
  fi
  _HOOK_LINES="$_hv_out"
}

# _settings_local_preflight <root> -- DGN-1250 landing condition 3 (DGN-1665).
# The wire step writes the INSTANCE-owned .claude/settings.local.json. ABSENT
# is fine and expected (the step seeds it). What is not fine is a sibling that
# exists and cannot be rewritten -- a directory, or bytes that do not parse as
# a JSON object. Checked in BOTH preflights (contract class and legacy) so the
# damage is named before the package copy lands, instead of surfacing as a
# refusal half-way through a delivery that already wrote files.
# Verdict is returned via a global (a $(...) caller would subshell a _fail).
_SETTINGS_LOCAL_VERDICT=""
_settings_local_preflight() { # -> 0 usable / 1 damaged
  local root="$1" f="$1/.claude/settings.local.json"
  _SETTINGS_LOCAL_VERDICT=""
  [[ -e "$f" ]] || return 0
  if [[ ! -f "$f" ]]; then
    _SETTINGS_LOCAL_VERDICT=".claude/settings.local.json exists but is not a regular file: $f (the instance-owned hook surface -- resolve manually)"
    return 1
  fi
  if [[ -n "${PACK_PY3:-}" ]] && ! "$PACK_PY3" -c "import json,sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if isinstance(d, dict) else 1)" "$f" >/dev/null 2>&1; then
    _SETTINGS_LOCAL_VERDICT=".claude/settings.local.json does not parse as a JSON object: $f (hook wiring would refuse mid-install; fix or remove the file)"
    return 1
  fi
  return 0
}

_hooks_wire() { # _hooks_wire <label> <settings_local_json> <root> <validated-lines>
  # Idempotent hook wiring -- same scan+append logic the old STEP 5 used
  # (entry shape {"hooks":[{"type":"command","command",...,"timeout"}]}),
  # parameterized over (event, script, timeout) and done in ONE python pass
  # with a single final write: every hook is re-validated against the
  # DELIVERED file before the first byte changes, so a refused run leaves
  # both settings files untouched (no partial write).
  #
  # TARGET (DGN-1250 수리안 1, DGN-1665): .claude/settings.local.json, NOT
  # .claude/settings.json. The framework OWNS settings.json and rewrites it
  # WHOLESALE from the template on every update (update.sh section 3g,
  # DGN-359 hooks split) -- a pack hook wired there is delivered, fires, and
  # is then erased by the next framework update with no error anywhere: the
  # declaration survives and only the wiring disappears. DGN-1250 recorded
  # six recurrences of exactly that. settings.local.json is instance-owned,
  # merged natively by Claude Code, and update.sh NEVER writes it.
  #
  # Three things make that repoint landable (all three, one change --
  # DGN-1250 landing conditions; landing the repoint alone hard-fails the
  # next mint, because the target file did not exist anywhere by design):
  #   1. create-if-missing: the target is seeded with {} when absent.
  #   2. the template ships .claude/settings.local.json (mint scaffolds it
  #      write-if-absent, scripts/mint.sh 1a3) -- so a newborn has it.
  #   3. the preflight checks BOTH files (parse-checks the sibling) so a
  #      damaged instance fails early and by name, not mid-install.
  # The "(mint incomplete?)" signal stays, moved onto the framework-owned
  # SIBLING (settings.json): that file is mint's product and its absence
  # really does mean an incomplete mint. The instance-owned target's absence
  # does not, and must never be reported as one.
  #
  # MIGRATION de-dup: hook files MERGE additively, so the same command
  # present in BOTH files is registered TWICE (measured 2026-09-23 on the
  # retired newborn: a {"decision":"block"} Stop hook armed to fire twice).
  # An instance wired by the pre-repoint installer carries this pack's hook
  # in settings.json; this step moves it -- the exact same command is
  # removed from settings.json as it lands in settings.local.json, so the
  # union stays identical and the duplicate count stays 0. ONLY commands
  # this install is wiring are touched; nothing else in the framework file
  # is read or rewritten.
  local label="$1" settings="$2" root="$3" lines="$4"
  local fw_settings; fw_settings="$(dirname "$settings")/settings.json"
  [[ -f "$fw_settings" ]] || _fail "$label: framework settings.json not found at $fw_settings (mint incomplete?)"
  if [[ ! -f "$settings" ]]; then
    mkdir -p "$(dirname "$settings")"
    printf '{}\n' > "$settings"
    _log "  [hook-wire] created $settings (instance-owned hook surface, DGN-1250)"
  fi
  local _hw_out="" _hw_rc=0
  _hw_out="$("$PACK_PY3" - "$settings" "$root" "$lines" "$fw_settings" <<'PYEOF'
import json, os, sys

settings_path, root, lines = sys.argv[1], sys.argv[2], sys.argv[3]
fw_path = sys.argv[4]

wanted = []
for ln in lines.splitlines():
    if not ln.strip():
        continue
    ev, sc, to = ln.split("\t")
    dst = os.path.join(root, sc)
    if not os.path.isfile(dst):
        # Declared and payload-validated but NOT delivered to the instance
        # (e.g. the owning category was not declared, so the copy step never
        # ran). Wiring a dangling command would fail at session time with
        # 'file not found' -- the exact two-gates trap this step replaces.
        print("HOOKS-WIRE-FAIL: declared hook script was not delivered to the instance: %s "
              "(declare the category that ships it, or fix the payload)" % dst)
        sys.exit(3)
    interp = "/usr/bin/python3" if sc.endswith(".py") else "/bin/bash"
    wanted.append((ev, "%s %s" % (interp, dst), int(to)))

try:
    with open(settings_path) as f:
        s = json.load(f)
except ValueError as e:
    print("HOOKS-WIRE-FAIL: %s is not valid JSON (%s) -- refusing to rewrite it"
          % (settings_path, e))
    sys.exit(3)
if not isinstance(s, dict):
    print("HOOKS-WIRE-FAIL: %s is not a JSON object -- refusing to rewrite it" % settings_path)
    sys.exit(3)

hooks = s.setdefault("hooks", {})
added = 0
for ev, cmd, to in wanted:
    ev_list = hooks.setdefault(ev, [])
    wired = False
    for entry in ev_list:
        # entry may be {"hooks": [...]} or direct {"type": "command", "command": ...}
        if isinstance(entry, dict):
            if entry.get("command") == cmd:
                wired = True
                break
            for h in entry.get("hooks", []):
                if isinstance(h, dict) and h.get("command") == cmd:
                    wired = True
                    break
            if wired:
                break
    if wired:
        print("[hook-wire] already wired (idempotent): %s -> %s" % (ev, cmd))
        continue
    ev_list.append({"hooks": [{"type": "command", "command": cmd, "timeout": to}]})
    print("[hook-wire] added %s -> %s" % (ev, cmd))
    added += 1

if added:
    with open(settings_path, "w") as f:
        json.dump(s, f, indent=2)
        f.write("\n")

# ---- migration de-dup against the framework-owned sibling -------------------
# Read-mostly: the sibling is only rewritten when it actually carries one of
# the commands just wired here. A malformed framework file is NOT fatal (it
# is not the file this step owns, and the wiring above already landed) -- it is
# reported and left alone.
moved = []
try:
    with open(fw_path) as f:
        fw = json.load(f)
except Exception as e:
    print("[hook-wire] WARN could not read %s for duplicate check (%s) -- "
          "a pre-repoint copy of these hooks may still be registered there" % (fw_path, e))
    fw = None
if isinstance(fw, dict) and isinstance(fw.get("hooks"), dict):
    want_cmds = set(cmd for _, cmd, _ in wanted)
    fw_hooks = fw["hooks"]
    for ev in list(fw_hooks):
        ev_list = fw_hooks.get(ev)
        if not isinstance(ev_list, list):
            continue
        kept_entries = []
        touched = False   # an entry was dropped OR rewritten in place
        for entry in ev_list:
            if not isinstance(entry, dict):
                kept_entries.append(entry)
                continue
            if entry.get("command") in want_cmds:
                moved.append((ev, entry.get("command")))
                touched = True
                continue
            inner = entry.get("hooks")
            if isinstance(inner, list):
                kept_inner = []
                for h in inner:
                    if isinstance(h, dict) and h.get("command") in want_cmds:
                        moved.append((ev, h.get("command")))
                        continue
                    kept_inner.append(h)
                if len(kept_inner) != len(inner):
                    # A framework block commonly groups SEVERAL commands; the
                    # rewrite must land even when the block count is unchanged
                    # (the first shape of this code only wrote back when an
                    # entry disappeared, so a grouped duplicate survived).
                    touched = True
                    if not kept_inner:
                        continue  # block held nothing else -> drop the block
                    entry = dict(entry, hooks=kept_inner)
            kept_entries.append(entry)
        if touched:
            if kept_entries:
                fw_hooks[ev] = kept_entries
            else:
                del fw_hooks[ev]
if moved:
    for ev, cmd in moved:
        print("[hook-wire] migrated OUT of %s: %s -> %s (framework file is "
              "regenerated wholesale; duplicate registration removed)" % (fw_path, ev, cmd))
    with open(fw_path, "w") as f:
        json.dump(fw, f, indent=2)
        f.write("\n")
print("HOOKS-WIRED: added=%d already=%d migrated=%d" % (added, len(wanted) - added, len(moved)))
PYEOF
)" || _hw_rc=$?
  while IFS= read -r _hw_l; do [[ -n "$_hw_l" ]] && _log "  $_hw_l"; done <<< "$_hw_out"
  if [[ "$_hw_rc" -ne 0 ]]; then
    local _hw_msg
    _hw_msg="$(grep '^HOOKS-WIRE-FAIL: ' <<< "$_hw_out" | head -1 || true)"
    _fail "$label: hook wiring REFUSED -- ${_hw_msg#HOOKS-WIRE-FAIL: }"
  fi
  # verdict for the caller: "run" when something was added, else "already-present"
  _HOOKS_WIRE_OUTCOME="already-present"
  grep -q '^HOOKS-WIRED: added=0 ' <<< "$_hw_out" || _HOOKS_WIRE_OUTCOME="run"
}

# _frag_version_guard <label> -- DGN-1467 grill #3. Runs when a fragment
# apply came back `already-present` on a NON-upgrade install: the block on
# disk stays whatever version wrote it, so if the ledger is about to record
# a DIFFERENT version the record would contradict the delivered reality
# (false green type 2: fragment@old, ledger@new, census `run`). Same-version
# re-install stays a quiet idempotent skip.
_frag_version_guard() {
  local label="$1" _fv_prev
  _fv_prev="$(sed -n 's/^DOGANY_PACKS=//p' "$ROOT/.instance.conf" 2>/dev/null | head -1 \
    | tr ',' '\n' | sed -n "s/^${PACK_ID}@//p" | head -1)"
  if [[ -n "$_fv_prev" && "$_fv_prev" != "$RECORD_VERSION" ]]; then
    _fail "$label: fragment block already present from $PACK_ID@$_fv_prev but this install would record @$RECORD_VERSION -- a non-upgrade install cannot replace a fragment block (idempotent skip), so the ledger would claim a version the delivered block does not have. Re-run with --upgrade (record must match delivered reality, DGN-1467)"
  fi
}

# _mint_residue_check <label> <file> -- DGN-1467 grill #4. _subst_mint_tokens
# skips the identity tokens when the instance identity is incomplete; for a
# contract fragment that residue would land verbatim in config/agent.conf or
# the persona file at rc=0 (publish-side C5 only forbids the 4 persona
# tokens -- __AGENT_PREFIX__/__AGENT_LANG__ are shippable). The "any residue
# FAILs" promise is enforced HERE for delivered fragments because the
# contract selftest lane skips G4 (and G4 only reads SKILL.md anyway). Same
# 7-token alternation as the G4 check (knowledge_selftest.sh).
_mint_residue_check() {
  local label="$1" f="$2" _mr_res
  _mr_res="$(grep -oE '__(PROJECT_ROOT|AGENT_NAME|AGENT_LABEL|USER_LABEL|AGENT_PREFIX|HOME|AGENT_LANG)__' "$f" 2>/dev/null | sort -u | tr '\n' ' ' || true)"
  if [[ -n "$_mr_res" ]]; then
    _fail "$label: unrendered mint token residue in the rendered fragment: ${_mr_res}-- instance identity incomplete (.instance.conf)? Complete the identity, then re-run (any residue FAILs the delivery, DGN-1467)"
  fi
}

# ---------------------------------------------------------------------------
# DGN-227 B3/P25: installed-files ledger (config/packs/<id>.files).
# Single record function called at EVERY copy point (H1-9: the ledger is not
# a side product -- every install write funnels through _ledger_record).
# The ledger is (a) the --upgrade removal-diff source (D2) and (b) the
# preserve-reconcile verdict source (D1).
#
# DGN-1526 R1: hoisted here, ahead of the pack-class dispatch. It used to be
# defined below the contract-class `exit 0`, so a kit/pack install recorded
# NOTHING (0 record calls on the contract path vs 26 on the legacy path).
# The contract path now records every whole-file write through _ct_deliver.
# Record only: nothing reads a contract ledger for removal -- the D2
# stale-removal diff and _preserve_reconcile stay legacy-path steps.
# Header: install time + pack revision (the source commit the payload came
# from) + source + class. Comment rows ('# held-local: p' / '# merged: p')
# record what the install deliberately did NOT write as a whole file;
# _ledger_paths strips them, so the D1/D2 readers see paths only.
# ---------------------------------------------------------------------------
LEDGER_DIR="$ROOT/config/packs"
LEDGER_FILE="$LEDGER_DIR/$PACK_ID.files"
LEDGER_STAGE=""
LEDGER_NOTE_STAGE=""

_ledger_record() { # _ledger_record <root-relative-path>
  [[ -n "$LEDGER_STAGE" ]] || LEDGER_STAGE="$(mktemp)"
  printf '%s\n' "$1" >> "$LEDGER_STAGE"
}

_ledger_note() { # _ledger_note <kind> <root-relative-path> -- comment row
  [[ -n "$LEDGER_NOTE_STAGE" ]] || LEDGER_NOTE_STAGE="$(mktemp)"
  printf '# %s: %s\n' "$1" "$2" >> "$LEDGER_NOTE_STAGE"
}

# _pack_revision -- the commit the payload bytes came from. Tag-sourced
# installs name the tag's commit (the sealed tree, DGN-1698); a work-tree
# install names HEAD, '+dirty' when the pack dir has uncommitted changes.
# Non-git sources (tar lane, fixtures) have no revision: say so.
_pack_revision() {
  local top rev
  if ! top="$(git -C "$PACKAGE_SRC_DIR" rev-parse --show-toplevel 2>/dev/null)" || [[ -z "$top" ]]; then
    printf 'none (non-git source)'
    return 0
  fi
  case "${EVIDENCE_SOURCE:-}" in
    tag:*)
      rev="$(git -C "$top" rev-parse -q --verify "refs/tags/${EVIDENCE_SOURCE#tag:}^{commit}" 2>/dev/null || true)"
      printf '%s' "${rev:-unresolved (tag ${EVIDENCE_SOURCE#tag:})}"
      ;;
    *)
      rev="$(git -C "$top" rev-parse -q --verify 'HEAD^{commit}' 2>/dev/null || true)"
      if [[ -z "$rev" ]]; then
        printf 'unresolved (no HEAD)'
      elif [[ -n "$(git -C "$PACKAGE_SRC_DIR" status --porcelain -- . 2>/dev/null | head -1)" ]]; then
        printf '%s+dirty' "$rev"
      else
        printf '%s' "$rev"
      fi
      ;;
  esac
}

_ledger_finalize() {
  mkdir -p "$LEDGER_DIR"
  {
    echo "# pack install ledger -- DGN-227 B3/P25"
    echo "# pack: $PACK_ID"
    echo "# pack_version: $PACK_VERSION"
    echo "# record_version: $RECORD_VERSION"
    echo "# pack_revision: $(_pack_revision)"
    echo "# source: ${EVIDENCE_SOURCE:-none}"
    echo "# class: ${PACK_CLASS:-legacy}"
    echo "# installed: $(date '+%Y-%m-%dT%H:%M:%S%z')"
    if [[ -n "$LEDGER_NOTE_STAGE" && -s "$LEDGER_NOTE_STAGE" ]]; then
      sort -u "$LEDGER_NOTE_STAGE"
    fi
    if [[ -n "$LEDGER_STAGE" && -s "$LEDGER_STAGE" ]]; then
      sort -u "$LEDGER_STAGE"
    fi
  } > "$LEDGER_FILE"
  [[ -n "$LEDGER_STAGE" ]] && rm -f "$LEDGER_STAGE"
  [[ -n "$LEDGER_NOTE_STAGE" ]] && rm -f "$LEDGER_NOTE_STAGE"
  _log "  ledger written: config/packs/$PACK_ID.files"
}

# _ledger_paths <file> -- entries only (comments stripped).
_ledger_paths() {
  [[ -f "$1" ]] || return 0
  grep -v '^#' "$1" | grep -v '^[[:space:]]*$' || true
}

# ---------------------------------------------------------------------------
# DGN-1526 R2/R4: the two layers, named.
#   shipped -- bytes a pack (or the framework) delivers and owns. Every
#              payload file is shipped-layer unless the owner claimed it.
#              Pack-tagged preserve lines ('# pack:<id>', legacy
#              '# pack-owned: <id>') are shipped-layer bookkeeping.
#   local   -- owner-made bytes no installer overwrites: an UNTAGGED line in
#              .claude/.dogany-preserve (hand-written, or registered with the
#              instance verb routines/skill-layer.py register).
# update.sh already honors every preserve line; the contract-class path read
# none of them. It now loads the local layer once and (a) passes it to the
# content-drift scanner as --preserve (lib/content_drift.py, "lockstep with
# .dogany-preserve") and (b) skips the copy of a held file that exists
# (_ct_deliver). A held path with no live file is still delivered: there is
# nothing to lose, and the evidence anchors expect the file.
# Parsing parity with update.sh PRESERVE_ENTRIES: strip '#...', trim, drop a
# leading './' or '/', refuse '..'.
# ---------------------------------------------------------------------------
LOCAL_LAYER_ENTRIES=()
_local_layer_load() {
  local pf="$ROOT/.claude/.dogany-preserve" raw line
  LOCAL_LAYER_ENTRIES=()
  [[ -f "$pf" ]] || return 0
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    case "$raw" in
      *"# pack:"*|*"# pack-owned:"*) continue ;;
    esac
    line="${raw%%#*}"
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [[ -n "$line" ]] || continue
    line="${line#./}"
    line="${line#/}"
    case "$line" in *..*) _log "  local layer: ignoring unsafe preserve entry: $line"; continue ;; esac
    LOCAL_LAYER_ENTRIES[${#LOCAL_LAYER_ENTRIES[@]}]="$line"
  done < "$pf"
}

# _local_layer_holds <root-relative-path> -- 0 when an owner entry covers it
# (exact file, or a trailing-'/' directory prefix; update.sh is_preserved).
_local_layer_holds() {
  local rel="$1" e
  for e in ${LOCAL_LAYER_ENTRIES[@]+"${LOCAL_LAYER_ENTRIES[@]}"}; do
    [[ "$e" == "$rel" ]] && return 0
    case "$e" in */) [[ "$rel" == "$e"* ]] && return 0 ;; esac
  done
  return 1
}

# _local_layer_touches <root-relative-dir> -- 0 when any owner entry sits
# inside the dir or covers it (used where a whole dir would be moved).
_local_layer_touches() {
  local dir="${1%/}/" e
  for e in ${LOCAL_LAYER_ENTRIES[@]+"${LOCAL_LAYER_ENTRIES[@]}"}; do
    [[ "$e" == "$dir"* ]] && return 0
    case "$e" in */) [[ "$dir" == "$e"* ]] && return 0 ;; esac
  done
  return 1
}

# _ct_deliver <src> <root-relative-dst> -- the contract-class whole-file copy
# point (R1 record + R2 hold). rc 1 = held by the local layer, nothing
# written (callers `|| continue` past their post-copy steps); rc 0 = copied
# and recorded.
# A write failure is FATAL here, explicitly: every caller runs this as
# `_ct_deliver ... || continue`, and bash suspends `set -e` inside a function
# called from an `||` list. Without the explicit _fail a failed mkdir/cp was
# logged and SKIPPED, the install ran on and recorded the new version over a
# half-copied member (DGN-1517 R7: a failed crew member must stay untouched;
# the inline `cp -f` this replaced aborted under `set -e`).
# The copy is atomic (DGN-1845, lib/atomic_write.sh): a live turn importing
# the target mid-install sees the whole old file or the whole new one --
# measured 2026-10-04: the in-place `cp -f` handed a turn a half-written
# database module (SyntaxError).
_ct_deliver() {
  local src="$1" rel="$2"
  if _local_layer_holds "$rel" && [[ -e "$ROOT/$rel" || -L "$ROOT/$rel" ]]; then
    _log "  local layer: HELD $rel (owner entry in .claude/.dogany-preserve) -- payload copy NOT written"
    _ledger_note held-local "$rel"
    return 1
  fi
  mkdir -p "$(dirname "$ROOT/$rel")" || _fail "$PACK_CLASS: cannot create the directory for $rel -- install aborted, no further writes"
  atomic_cp "$src" "$ROOT/$rel" || _fail "$PACK_CLASS: copy failed for $rel -- install aborted, no further writes"
  _ledger_record "$rel"
  return 0
}

# ---------- pack class dispatch (DGN-1018 §5-B) ------------------------------
# Contract class axis: kind=kit OR (kind=pack AND contract_version present)
# takes the payload_root pipeline below. A legacy dev pack (kind absent AND
# contract_version absent, measured) satisfies NEITHER condition and keeps the
# agent/module path below byte-identical -- no-regression hard requirement.
# Contract presence uses the same "genuinely observed" rule as the
# requires_kit gate above (None or "" == absent).
PACK_CONTRACT_PRESENT="$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get('contract_version'); print(0 if (v is None or v=='') else 1)" "$MANIFEST" 2>/dev/null || echo 0)"
PACK_CLASS=""
if [[ "$PACK_KIND" == "kit" ]]; then
  PACK_CLASS="kit"
elif [[ "$PACK_KIND" == "pack" && "$PACK_CONTRACT_PRESENT" -eq 1 ]]; then
  PACK_CLASS="pack"
fi

# completion predicate (DGN-1374): only a kit can declare a data-dependent
# completion verb (behavior packs never own a DB lane). Default empty so the
# variable is defined regardless of class -- populated below in the kit
# branch only.
KIT_COMPLETION_VERB=""
KIT_COMPLETE=1

# ---- poisoned-kind fail-closed (compat-lint C-KIND twin, DGN-681/DGN-1018) --
# INDEPENDENT DUPLICATION of compat-lint's C-KIND vocabulary check, same
# DGN-1002 lockstep precedent as the inline re-validation blocks below: the
# install-side compat-lint gate is structurally fail-open (WARN+skip when
# compat-lint.sh is absent), so a third-party pack that never ran the lint
# arrives here unchecked -- and install-side is the ONLY gate a third-party
# distribution passes through.
#
# The two conditions above match on the EXACT strings "kit" / "pack". A
# contract manifest (contract_version present) whose kind is anything else
# satisfies NEITHER, leaving PACK_CLASS empty, and would fall silently through
# to the legacy agent/module path below -- a contract pack installed down the
# wrong pipeline. compat-lint already calls this out fail-closed ("a poisoned
# class value must not drive class-branched allowlists or install dispatch")
# and ABORTs; without this branch the installer alone could not.
#
# Scope is guarded by PACK_CONTRACT_PRESENT: a genuine legacy pack (kind
# absent AND contract absent) is untouched, keeping the agent/module path
# byte-identical (DGN-1018 5-B no-regression hard requirement).
# Undefined = refuse (DGN-1004). Verdict-identical with compat-lint C-KIND.
if [[ -z "$PACK_CLASS" && "$PACK_CONTRACT_PRESENT" -eq 1 ]]; then
  # Same 'kind' description probe as compat-lint C-KIND (absent / non-string /
  # the quoted string) so both sides name the damage identically.
  _kind_verdict="$("$PACK_PY3" -c "
import json, sys
d = json.load(open(sys.argv[1]))
if 'kind' not in d:
    print('BLOCK:absent')
elif not isinstance(d['kind'], str):
    print('BLOCK:non-string: %r' % (d['kind'],))
else:
    print(\"BLOCK:'%s'\" % d['kind'])
" "$MANIFEST" 2>/dev/null || echo "BLOCK:unreadable")"
  _fail "kind gate: manifest kind ${_kind_verdict#BLOCK:} invalid -- a contract pack (contract_version present) must declare kind exactly 'kit' or 'pack' ('agentpack' is a repo-naming segment, not a manifest kind); refusing to fall through to the legacy agent/module path (verdict-identical with compat-lint C-KIND, DGN-1018 2; fail-closed, no warn lane)"
fi

if [[ -n "$PACK_CLASS" ]]; then
  # Logging setup: kit install uses the same log directory convention.
  LOG_DIR="$ROOT/.telegram_bot/logs"
  LOG_FILE="$LOG_DIR/pack-install.log"
  # DGN-1710: a dry-run writes NOTHING into the instance -- not even the log
  # dir (the legacy path creates it only after its DRY exit, :5470). _log
  # never touches LOG_DIR under DRY (stderr only), so skipping the mkdir
  # there loses nothing; a real run still keeps its preflight trail.
  [[ "$DRY" -eq 1 ]] || mkdir -p "$LOG_DIR"

  if [[ "$PACK_CLASS" == "kit" ]]; then
    _log "=== kit-install START pack=$PACK_ID provides_kit=$KIT_PROVIDES slug=$SLUG root=$ROOT ==="
  else
    _log "=== pack-install START (behavior-pack profile, DGN-1018) pack=$PACK_ID slug=$SLUG root=$ROOT ==="
  fi

  # ---- preflight ----
  KIT_PAYLOAD_DIR="$PACKAGE_DIR/$KIT_PAYLOAD_ROOT"
  [[ -d "$KIT_PAYLOAD_DIR" ]] || _fail "kit payload root not found: $KIT_PAYLOAD_DIR"
  [[ -d "$ROOT" ]] || _fail "instance root not found: $ROOT"
  # DGN-1526 R2: the owner's local layer (untagged .dogany-preserve lines),
  # read once, before the content-drift guard and every copy step.
  _local_layer_load
  if [[ "${#LOCAL_LAYER_ENTRIES[@]}" -gt 0 ]]; then
    _log "$PACK_CLASS: local layer: ${#LOCAL_LAYER_ENTRIES[@]} owner entr(y/ies) in .claude/.dogany-preserve -- held paths are neither judged nor overwritten:"
    for _ll_e in "${LOCAL_LAYER_ENTRIES[@]}"; do _log "  local layer: $_ll_e"; done
  else
    _log "$PACK_CLASS: local layer: no owner entries in .claude/.dogany-preserve"
  fi
  # Hook wire target, same check the legacy preflight runs (DGN-1250 landing
  # condition 3): a damaged settings.local.json must be named HERE, before the
  # payload copy, not by the wiring step after files have already landed.
  _settings_local_preflight "$ROOT" || _fail "preflight: $_SETTINGS_LOCAL_VERDICT"

  # knowledge_warehouse has no kit branch (kind=kit forbids the field
  # entirely, checked below) -- initialize here so both branches see it
  # defined under 'set -u' (the CD_ARGS/dry-run reads further down are
  # unconditional on PACK_CLASS).
  KNOW_WH=""

  if [[ "$PACK_CLASS" == "pack" ]]; then
  # ---- behavior-pack identity + inline re-validation (DGN-1018 §5-B) --------
  # A behavior pack has NO kit identity (§3): KIT_NAME stays empty, the DB
  # lane is off, and the service surface derives from service_namespace.
  #
  # INDEPENDENT DUPLICATION of the compat-lint C-KIND / C1 declaration rules
  # (DGN-1002 lockstep precedent, same as the KIT_NAME block in the kit
  # branch below): the compat-lint gate further down is structurally
  # fail-open (WARN+skip when compat-lint.sh is absent), so the installer
  # re-validates kind / service_namespace / db_lane on its own. Verdicts
  # must stay verdict-identical with compat-lint; if either side changes,
  # change the other. Damage = BLOCK (fail-closed, no warn lane).
  KIT_NAME=""
  KIT_DB_LANE=0
  KIT_SCHEDULES=0
  SERVICE_NS=""
  _pack_inline_verdict="$("$PACK_PY3" - "$MANIFEST" "$PACK_TOKEN_CORE" "${PACK_RESERVED_TOKENS[*]}" <<'PYEOF'
import json, re, sys

def out(v):
    print(v)
    sys.exit(0)

# DGN-1018 RR2: regex core + reserved list are shared data, passed as argv
# since a heredoc body cannot see bash variables -- see
# lib/pack_token_grammar.sh.
_TOKEN_CORE = sys.argv[2]
_RESERVED_ARGV = tuple(sys.argv[3].split())

try:
    d = json.load(open(sys.argv[1]))
    if not isinstance(d, dict):
        raise ValueError("manifest top-level JSON is not an object")
except Exception as e:
    out("BLOCK:pack-manifest.json unreadable (%s)" % e)

# kind re-check (compat-lint C-KIND twin): the dispatch matched kind=="pack";
# this re-read guards the value is a plain exact string.
if d.get("kind") != "pack":
    out("BLOCK:kind is not exactly 'pack' (%r)" % (d.get("kind"),))

# provides_kit on kind=pack = refused (S4b symmetry: provides_kit <=> kind=kit;
# compat-lint C1 twin).
if "provides_kit" in d:
    out("BLOCK:kind=pack manifest declares provides_kit -- a kit-providing "
        "pack is kind=kit (S4b symmetry)")

# Grammar single source: provides_kit rules (compat-lint _kit_token_ok twin).
# \Z (not $): python '$' also matches BEFORE a trailing newline, which the
# bash [[ =~ ]] twin rejects -- '$' here would accept "ns\n" and diverge.
NAME_RE = re.compile('^' + _TOKEN_CORE + r'\Z')
RESERVED = _RESERVED_ARGV

# service_namespace: optional single string (DGN-1018 §4).
ns = ""
if "service_namespace" in d:
    v = d["service_namespace"]
    if not isinstance(v, str):
        out("BLOCK:service_namespace must be a single string, got %r" % (v,))
    if not NAME_RE.match(v) or v in RESERVED:
        out("BLOCK:service_namespace invalid: '%s' -- must match "
            "^[a-z][a-z0-9_-]{0,31}$ and not be a framework-reserved name" % v)
    rk = d.get("requires_kit")
    rk_kit = rk.get("kit") if isinstance(rk, dict) else None
    if isinstance(rk_kit, str) and v == rk_kit:
        out("BLOCK:service_namespace '%s' equals requires_kit.kit -- "
            "dependency-kit service surface impersonation refused" % v)
    ns = v

# capabilities.db_lane / capabilities.schedules for kind=pack: absent =>
# false; true => refused (both are kit-exclusive surfaces); malformed =>
# refused (compat-lint C1 twin).
caps = d.get("capabilities")
if caps is not None:
    if not isinstance(caps, dict):
        out("BLOCK:capabilities is not an object")
    unknown = sorted(set(caps) - {"db_lane", "schedules"})
    if unknown:
        out("BLOCK:unknown capabilities key(s): %s"
            % ", ".join(repr(k) for k in unknown))
    v = caps.get("db_lane")
    if v is not None and not isinstance(v, bool):
        out("BLOCK:capabilities.db_lane must be a JSON boolean, got %r" % (v,))
    if v is True:
        out("BLOCK:kind=pack declares capabilities.db_lane=true -- the DB "
            "lane is a kit-exclusive surface")
    sc = caps.get("schedules")
    if sc is not None and not isinstance(sc, bool):
        out("BLOCK:capabilities.schedules must be a JSON boolean, got %r" % (sc,))
    if sc is True:
        out("BLOCK:kind=pack declares capabilities.schedules=true -- "
            "scheduled routine delivery (routines/*.plist) is a "
            "kit-exclusive surface (DGN-1402)")

out("PASS:%s" % ns)
PYEOF
)" || _pack_inline_verdict="BLOCK:behavior-pack inline validation crashed -- fail closed"
  case "$_pack_inline_verdict" in
    PASS:*) SERVICE_NS="${_pack_inline_verdict#PASS:}" ;;
    BLOCK:*) _fail "pack: inline re-validation: ${_pack_inline_verdict#BLOCK:} (verdict-identical with compat-lint, DGN-1018 §5-B; fail-closed)" ;;
    *) _fail "pack: inline re-validation: unrecognized verdict '$_pack_inline_verdict' -- fail closed" ;;
  esac
  # ns payload-surface rules (need KIT_PAYLOAD_DIR, so checked here):
  # declared but payload/service/<ns>/ absent = dead declaration (C1 twin);
  # service/ files shipped without a declaration = refused (C4 twin --
  # declaration precedes surface).
  if [[ -n "$SERVICE_NS" && ! -d "$KIT_PAYLOAD_DIR/service/$SERVICE_NS" ]]; then
    _fail "pack: service_namespace '$SERVICE_NS' declared but payload/service/$SERVICE_NS/ does not exist (dead declaration; verdict-identical with compat-lint C1)"
  fi
  if [[ -z "$SERVICE_NS" && -d "$KIT_PAYLOAD_DIR/service" ]]; then
    _svc_files="$(find "$KIT_PAYLOAD_DIR/service" -type f ! -name '.gitkeep' | grep -c . || true)"
    if [[ "${_svc_files:-0}" -gt 0 ]]; then
      _fail "pack: payload/service/ ships ${_svc_files} file(s) but no service_namespace is declared -- declaration precedes surface (verdict-identical with compat-lint C4)"
    fi
  fi

  # ---- knowledge_warehouse: inline re-validation (DGN-1426, verdict-
  # identical twin of compat-lint C1/C4). Optional single string, kind=pack
  # only. See compat-lint.sh for the full doctrine comment.
  KNOW_WH=""
  _know_inline_verdict="$("$PACK_PY3" - "$MANIFEST" "$PACK_TOKEN_CORE" "${PACK_RESERVED_TOKENS[*]}" <<'PYEOF'
import re, sys, json

def out(v):
    print(v)
    sys.exit(0)

_TOKEN_CORE = sys.argv[2]
_RESERVED_ARGV = tuple(sys.argv[3].split())
NAME_RE = re.compile('^' + _TOKEN_CORE + r'\Z')

d = json.load(open(sys.argv[1]))
wh = ""
if "knowledge_warehouse" in d:
    v = d["knowledge_warehouse"]
    if not isinstance(v, str):
        out("BLOCK:knowledge_warehouse must be a single string, got %r" % (v,))
    if not NAME_RE.match(v) or v in _RESERVED_ARGV:
        out("BLOCK:knowledge_warehouse invalid: '%s' -- must match "
            "^[a-z][a-z0-9_-]{0,31}$ and not be a framework-reserved name" % v)
    wh = v
out("PASS:%s" % wh)
PYEOF
)" || _know_inline_verdict="BLOCK:knowledge_warehouse inline validation crashed -- fail closed"
  case "$_know_inline_verdict" in
    PASS:*) KNOW_WH="${_know_inline_verdict#PASS:}" ;;
    BLOCK:*) _fail "pack: inline re-validation: ${_know_inline_verdict#BLOCK:} (verdict-identical with compat-lint C1, DGN-1426; fail-closed)" ;;
    *) _fail "pack: inline re-validation: unrecognized verdict '$_know_inline_verdict' -- fail closed" ;;
  esac
  # warehouse payload-surface rules (need KIT_PAYLOAD_DIR, so checked here):
  # declared but payload/knowledge/<name>/ absent = dead declaration (C1
  # twin); knowledge/ files shipped without a matching declaration = refused
  # (C4 twin -- declaration precedes surface).
  if [[ -n "$KNOW_WH" && ! -d "$KIT_PAYLOAD_DIR/knowledge/$KNOW_WH" ]]; then
    _fail "pack: knowledge_warehouse '$KNOW_WH' declared but payload/knowledge/$KNOW_WH/ does not exist (dead declaration; verdict-identical with compat-lint C1)"
  fi
  if [[ -d "$KIT_PAYLOAD_DIR/knowledge" ]]; then
    if [[ -z "$KNOW_WH" ]]; then
      _kw_files="$(find "$KIT_PAYLOAD_DIR/knowledge" -type f ! -name '.gitkeep' | grep -c . || true)"
      if [[ "${_kw_files:-0}" -gt 0 ]]; then
        _fail "pack: payload/knowledge/ ships ${_kw_files} file(s) but no knowledge_warehouse is declared -- declaration precedes surface (verdict-identical with compat-lint C4)"
      fi
    else
      while IFS= read -r _kw_dir; do
        _kw_name="$(basename "$_kw_dir")"
        if [[ "$_kw_name" != "$KNOW_WH" ]]; then
          _fail "pack: forbidden payload path (knowledge/$_kw_name does not match declared knowledge_warehouse='$KNOW_WH'; verdict-identical with compat-lint C4)"
        fi
      done < <(find "$KIT_PAYLOAD_DIR/knowledge" -mindepth 1 -maxdepth 1 -type d)
    fi
  fi
  _log "pack: behavior-pack profile: no kit identity; service_namespace='${SERVICE_NS:-}' db_lane=0 (DGN-1018 §5-B); knowledge_warehouse='${KNOW_WH:-}'"
  else

  # ---- kit identity: KIT_NAME from provides_kit (DGN-1003) --------------------
  # LOCKSTEP CONTRACT with scripts/pack/compat-lint.sh C1 (DGN-1002): same
  # absence verdict (key absent on kind=kit -> FATAL, DGN-1143 -- the legacy
  # "lifekit" default is REMOVED: zero published manifests relied on it, and
  # after the dec-145 kit rename it would silently drive every write path at
  # a nonexistent kit), same value regex, same
  # framework-reserved-name blacklist, same fail-closed direction. The
  # installer validates INDEPENDENTLY: the compat-lint gate below is skipped
  # when compat-lint.sh is absent, and KIT_NAME drives filesystem write paths
  # (database/<kit>.db, service/<kit>/, config/<kit>.conf, crews/<kit>/), so
  # trusting the caller to have gated the manifest would reopen the
  # path-injection surface. If either side of the contract changes, change
  # the other and keep the lockstep tests in
  # scripts/tests/test-pack-install-kit.sh (TL*) green.
  KIT_NAME=""
  _pk_present="$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if 'provides_kit' in d else 0)" "$MANIFEST" 2>/dev/null || echo 0)"
  if [[ "$_pk_present" -eq 0 ]]; then
    _fail "kit: manifest missing provides_kit -- the legacy 'lifekit' default was removed (DGN-1143: zero published manifests relied on it, and after the dec-145 kit rename it would silently resolve to a nonexistent kit). Declare provides_kit explicitly. (lockstep with compat-lint C1)"
  fi
  if [[ "$_pk_present" -eq 1 ]]; then
    # Same extraction semantics as compat-lint _mf_str (str/int/float pass
    # through as text, every other JSON type collapses to '' and fails the
    # regex below -- all-type fail-closed, DGN-1002 review note).
    KIT_NAME="$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get('provides_kit'); print(v if isinstance(v,(str,int,float)) else '')" "$MANIFEST" 2>/dev/null || echo "")"
    _kit_name_ok=1
    # Regex core + reserved list: DGN-1018 RR2 shared data (lib/pack_token_
    # grammar.sh) -- anchor stays local (bash form), see lib header.
    _kit_name_re='^'"${PACK_TOKEN_CORE}"'$'
    if [[ ! "$KIT_NAME" =~ $_kit_name_re ]]; then
      _kit_name_ok=0
    fi
    for _rsv in "${PACK_RESERVED_TOKENS[@]}"; do
      [[ "$KIT_NAME" == "$_rsv" ]] && _kit_name_ok=0
    done
    if [[ "$_kit_name_ok" -eq 0 ]]; then
      _fail "kit: provides_kit invalid: '$KIT_NAME' -- must match ^[a-z][a-z0-9_-]{0,31}\$ and not be a framework-reserved name (lockstep with compat-lint C1, DGN-1002/DGN-1003)"
    fi
  fi

  # ---- kit capability: db_lane (DGN-1003, lockstep with compat-lint C1) ------
  # capabilities block absent / empty / db_lane=true -> DB lane active (today's
  # behavior). db_lane=false -> db-init + migrate are capability-SKIPped, with
  # the same skip-smuggling refusal as compat-lint C3: shipping database/
  # files while declaring no DB lane is a contradiction, refused fail-closed.
  # Malformed block -> FATAL (compat-lint FAILs the pack too; verdict-identical).
  KIT_DB_LANE=1
  KIT_SCHEDULES=0
  _caps_out="$("$PACK_PY3" - "$MANIFEST" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
caps = d.get("capabilities")
if caps is None:
    print("absent"); print("absent"); sys.exit(0)
if not isinstance(caps, dict):
    print("invalid: capabilities is not an object"); sys.exit(0)
unknown = sorted(set(caps) - {"db_lane", "schedules"})
if unknown:
    print("invalid: unknown capabilities key(s): %s" % ", ".join(repr(k) for k in unknown)); sys.exit(0)
v = caps.get("db_lane")
if v is None:
    print("absent")   # empty object == nothing declared
elif not isinstance(v, bool):
    print("invalid: capabilities.db_lane must be a JSON boolean, got %r" % (v,)); sys.exit(0)
else:
    print("true" if v else "false")
s = caps.get("schedules")
if s is None:
    print("absent")
elif not isinstance(s, bool):
    print("invalid: capabilities.schedules must be a JSON boolean, got %r" % (s,)); sys.exit(0)
else:
    print("true" if s else "false")
PYEOF
)" || _caps_out=$'invalid: manifest JSON unreadable'
  _caps_verdict="$(printf '%s\n' "$_caps_out" | sed -n '1p')"
  _sched_verdict="$(printf '%s\n' "$_caps_out" | sed -n '2p')"
  case "$_caps_verdict" in
    absent|true) ;;                 # KIT_DB_LANE stays 1 -- today's behavior
    false)       KIT_DB_LANE=0 ;;
    *) _fail "kit: capabilities block malformed -- ${_caps_verdict#invalid: } (lockstep with compat-lint C1, fail closed)" ;;
  esac
  if [[ "$KIT_DB_LANE" -eq 0 && -d "$KIT_PAYLOAD_DIR/database" ]]; then
    _db_files="$(find "$KIT_PAYLOAD_DIR/database" -type f ! -name '.gitkeep' | grep -c . || true)"
    if [[ "${_db_files:-0}" -gt 0 ]]; then
      _fail "kit: manifest declares capabilities.db_lane=false but payload/database/ ships ${_db_files} file(s) -- undeclared DB lane, declaration contradicts payload (skip-smuggling refused; lockstep with compat-lint C3)"
    fi
  fi
  if [[ "$_caps_verdict" != invalid:* ]]; then
    case "$_sched_verdict" in
      absent|false) ;;               # KIT_SCHEDULES stays 0 -- no schedule surface
      true)         KIT_SCHEDULES=1 ;;
      *) _fail "kit: capabilities block malformed -- ${_sched_verdict#invalid: } (lockstep with compat-lint C1, fail closed)" ;;
    esac
  fi
  # capabilities.schedules dead declaration (DGN-1402, lockstep with
  # compat-lint C1 -- service_namespace/knowledge_warehouse precedent).
  if [[ "$KIT_SCHEDULES" -eq 1 ]]; then
    _sched_files=0
    if [[ -d "$KIT_PAYLOAD_DIR/routines" ]]; then
      _sched_files="$(find "$KIT_PAYLOAD_DIR/routines" -maxdepth 1 -type f -name '*.plist' | grep -c . || true)"
    fi
    if [[ "${_sched_files:-0}" -eq 0 ]]; then
      _fail "kit: manifest declares capabilities.schedules=true but payload/routines/*.plist does not exist (dead declaration; lockstep with compat-lint C1)"
    fi
  fi
  # service_namespace on kind=kit = dual truth-source, refused (DGN-1018 §4;
  # a kit's service namespace canonical source is provides_kit). Inline twin
  # of the compat-lint C1 rule -- the lint gate below may be absent.
  _ns_on_kit="$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if 'service_namespace' in d else 0)" "$MANIFEST" 2>/dev/null || echo 0)"
  if [[ "$_ns_on_kit" -eq 1 ]]; then
    _fail "kit: manifest declares service_namespace -- a kit's service namespace canonical source is provides_kit (dual truth-source refused; verdict-identical with compat-lint C1, DGN-1018)"
  fi

  # knowledge_warehouse on kind=kit = zero-consumer surface, refused
  # (DGN-1018 §4/DGN-1426). Inline twin of the compat-lint C1 rule.
  _kw_on_kit="$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if 'knowledge_warehouse' in d else 0)" "$MANIFEST" 2>/dev/null || echo 0)"
  if [[ "$_kw_on_kit" -eq 1 ]]; then
    _fail "kit: manifest declares knowledge_warehouse -- knowledge/ has zero kit consumers (dual-purpose refused; verdict-identical with compat-lint C1, DGN-1426)"
  fi

  # ---- DGN-1509 S5: --prepare -- stage a candidate generation, touch NOTHING
  # live (crew-coherence report §3(b) "전 멤버 후보를 비활성 위치에 준비" --
  # the prepare/migrate/activate split, SPEC v3 §3.1/§9 S5). ADDITIVE: no
  # caller in this repo invokes --prepare yet (S7's _apply_crew rewrite
  # would be the first), so this branch is unreachable from every existing
  # code path today -- zero behavior change for install/upgrade/dry-run.
  #
  # What it deliberately does NOT do (the "prepare" prohibitions this spec
  # stage carries, matching update_seed.sh's own non-interference
  # precedent): it does not touch database/<kit>.db (no db-init, no
  # migrate), does not touch any EXISTING real file outside .gen/ (no
  # kit_core copy to the live path), does not write crew.conf or a lease,
  # does not start/stop/restart anything. It stages a full copy of
  # payload/database/ into a per-txn generation directory and records a
  # manifest of what it staged. Activation (S7, not landed in this repo
  # yet) is a separate, later, atomic step that this function never
  # performs -- see SPEC v3 §3.1's "activate" primitive for that half.
  if [[ "$PREPARE" -eq 1 ]]; then
    if [[ "$KIT_DB_LANE" -eq 0 ]]; then
      _log "kit: prepare: capabilities.db_lane=false -- nothing to stage (no DB lane)"
      exit 0
    fi
    _gen_dir="$ROOT/database/.gen/$SOURCE_TAG"
    if [[ -d "$_gen_dir" ]]; then
      _log "kit: prepare: generation already staged ($_gen_dir) -- idempotent no-op (re-preparing the SAME candidate tag is not an error)"
      exit 0
    fi
    # Stage into a private tmp dir first, then ONE mv into the final
    # generation name -- a crash mid-copy leaves an orphaned .tmp-prepare.*
    # dir, never a half-populated $_gen_dir that a reader could mistake for
    # complete (SPEC v3 §3.0: readers key off the FINAL name existing, not
    # off individual file presence).
    _gen_tmp="$ROOT/database/.gen/.tmp-prepare.$$"
    rm -rf "$_gen_tmp"
    mkdir -p "$_gen_tmp"
    _entries_list=""
    if [[ -d "$KIT_PAYLOAD_DIR/database" ]]; then
      while IFS= read -r _src; do
        [[ -e "$_src" ]] || continue
        [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
        # Same enumeration convention as the kit_core copy step above --
        # the generation's entry LIST is a discovery of the payload
        # manifest at prepare time, never a hardcoded count (DGN-1509 v3
        # §3.1 G2: a hardcoded generation size silently stops covering a
        # payload that later grows).
        _rel="${_src#"$KIT_PAYLOAD_DIR/database/"}"
        _dst="$_gen_tmp/$_rel"
        mkdir -p "$(dirname "$_dst")"
        cp -f "$_src" "$_dst"
        _entries_list="${_entries_list:+$_entries_list$'\n'}$_rel"
        _log "  prepare: staged $_rel"
      done < <(find "$KIT_PAYLOAD_DIR/database" -type f)
    fi
    "$PACK_PY3" -c "
import datetime, json, sys
entries = [e for e in sys.argv[4].split(chr(10)) if e]
doc = {
    'txn': sys.argv[1],
    'pack_id': sys.argv[2],
    'pack_version': sys.argv[3],
    'prepared_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    'entries': entries,
}
json.dump(doc, open(sys.argv[5], 'w'), indent=2)
" "$SOURCE_TAG" "$PACK_ID" "$(_mf_field pack_version)" "$_entries_list" "$_gen_tmp/candidate.json" \
      || _fail "kit: prepare: candidate.json write failed"
    # SOURCE_TAG is a multi-segment tag (pack/<pid>/v<ver>), so $_gen_dir's
    # parent is itself nested -- the intermediate segments must exist before
    # mv can land the final directory (mv, like mkdir, is not -p; a missing
    # parent is ENOENT, not "target busy").
    mkdir -p "$(dirname "$_gen_dir")"
    if ! mv "$_gen_tmp" "$_gen_dir" 2>/dev/null; then
      rm -rf "$_gen_tmp"
      if [[ -d "$_gen_dir" ]]; then
        _log "kit: prepare: a concurrent prepare landed $_gen_dir first -- idempotent no-op"
      else
        _fail "kit: prepare: failed to land generation directory ($_gen_dir)"
      fi
    fi
    # Postcondition (SPEC v3 §3.0 rule: rc is not a postcondition -- a
    # primitive claiming atomicity/completion is verified by reading the
    # RESULT state, not by trusting its own rc). mv's rc=0 on this platform
    # is proven by the primitives selftest below; this line is the
    # defense against a future platform where it silently is not.
    [[ -d "$_gen_dir" && -f "$_gen_dir/candidate.json" ]] \
      || _fail "kit: prepare: postcondition failed -- $_gen_dir/candidate.json does not exist after staging"
    _log "kit: prepare: candidate generation staged -- $_gen_dir ($(printf '%s\n' "$_entries_list" | grep -c . || true) entries)"
    exit 0
  fi

  # ---- kit completion gate: completion_verb (DGN-1374, top-level/additive) ---
  # "Installed" (payload landed) and "complete" (usable -- its data-dependent
  # step has actually run) are different facts; today only the former is
  # tracked. A top-level key is used (not nested under capabilities) so this
  # is an additive manifest extension with no lockstep dual-implementation
  # requirement (capabilities' unknown-key fail-closed would force touching
  # compat-lint.sh too -- see packs/CONTRACT.md precedent for units/skills).
  # Value is a verb NAME ONLY, for the human-facing report below -- the
  # installer NEVER invokes it (that would make the installer execute a
  # data-mutating pack verb, exactly the risk this ticket keeps out of
  # scope). Absence means "no data-dependent step declared" -- installed ==
  # complete, unchanged behavior (same absence doctrine as requires_framework).
  KIT_COMPLETION_VERB="$("$PACK_PY3" -c "
import json, sys
d = json.load(open(sys.argv[1]))
v = d.get('completion_verb')
if v is None:
    print('')
elif isinstance(v, str) and v.strip():
    print(v.strip())
else:
    print('INVALID')
" "$MANIFEST" 2>/dev/null || echo "INVALID")"
  if [[ "$KIT_COMPLETION_VERB" == "INVALID" ]]; then
    _fail "kit: manifest field 'completion_verb' must be a non-empty string when present (DGN-1374)"
  fi
  if [[ -n "$KIT_COMPLETION_VERB" && "$KIT_DB_LANE" -eq 0 ]]; then
    _fail "kit: manifest declares completion_verb='$KIT_COMPLETION_VERB' but capabilities.db_lane=false -- no data lane to complete (contradiction refused, same shape as C3 skip-smuggling, DGN-1374)"
  fi
  _log "kit: kit-name: $KIT_NAME (provides_kit declared) db_lane=$KIT_DB_LANE schedules=$KIT_SCHEDULES completion_verb='${KIT_COMPLETION_VERB:-<none>}'"
  fi

  # DGN-956 crew-shared skill root (fork F1): keyed by the kit id -- the
  # sharing scope is "instances mounting the same kit" (lane crew). Lives
  # OUTSIDE any instance dir (root-relocation doctrine; same roof as the
  # shared kit DB under ~/.dogany/crews/<kit>/). DOGANY_SHARED_HOME override
  # exists for test hermeticity only (fixtures inject a mktemp home).
  # DGN-1018: a behavior pack has no kit identity, hence no crew-shared skill
  # root -- sharing_mode=share is refused for class=pack (undefined scope =
  # refuse, DGN-1004 direction) in the skills-bundle step below.
  SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
  # CREW_DIR: same literal as update_plan.sh:126 / update_apply.sh:233 /
  # update_seed.sh:70 -- a 4th copy, not a shared constant (DGN-1517 spec v1
  # §2.2 T-4, thin spot accepted rather than adding a lib for one line). The
  # crew-lockstep guard's evidence class (E1, DGN-1517 §2.1) needs it to
  # read the lease this install's own transaction wrote.
  CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
  if [[ "$PACK_CLASS" == "kit" ]]; then
    SHARED_ROOT="$SHARED_HOME/crews/$KIT_NAME/shared-skills"
  else
    SHARED_ROOT=""
  fi

  # DGN-1840/1843: resolve ONCE before guards, dry-run and evidence_begin.
  # Two sets from one resolution (kit_subset.py): _new_skill_names = bodies
  # delivered + declared (copies, drift, retirement); _active_skill_names =
  # activation links + evidence anchors. A delivered-only body is inert
  # storage that dogany-lifekit-setup "skill on" links without a reinstall.
  _new_skill_names="" _active_skill_names=""
  if [[ "$PACK_CLASS" == "kit" ]]; then
    _subset_out="$("$PACK_PY3" "$REPO_DIR/agents/.template/routines/lib/kit_subset.py" \
      --root "$ROOT" --payload "$KIT_PAYLOAD_DIR" --kit "$KIT_NAME" \
      --catalog "$CATALOG" --parent-pack "${DOGANY_RK_DOMAIN_PACK:-}")" \
      || _fail "kit: subset resolution failed -- nothing delivered (DGN-1840)"
    while IFS=$'\t' read -r _sd _sm; do
      [[ -n "$_sd" ]] || continue
      _new_skill_names+="$_sd"$'\n'
      if [[ "$_sm" == "activate" ]]; then _active_skill_names+="$_sd"$'\n'; fi
    done <<< "$_subset_out"
  elif [[ -d "$KIT_PAYLOAD_DIR/skills-bundle" ]]; then
    while IFS= read -r _sd; do
      _new_skill_names+="$(basename "$_sd")"$'\n'
    done < <(find "$KIT_PAYLOAD_DIR/skills-bundle" -mindepth 1 -maxdepth 1 -type d | sort)
    _active_skill_names="$_new_skill_names"
  fi
  _selected_skill_dirs() {
    local name
    while IFS= read -r name; do
      [[ -n "$name" ]] && printf '%s/skills-bundle/%s\n' "$KIT_PAYLOAD_DIR" "$name"
    done <<< "$_new_skill_names"
    return 0
  }
  _skill_is_active() { grep -qxF -- "$1" <<< "$_active_skill_names"; }

  # DGN-1843: same-instance ownership keeps the whole skill; a crew
  # peer keeps only the shared canonical, while this member's links retire.
  _retire_shared_root() {
    local name="$1" scan row peer owner target db self canonical crew_db
    [[ -n "$SHARED_ROOT" ]] || return 0
    . "$SCRIPT_DIR/lib/discover_instances.sh"
    scan="$(_discover_instances)" || {
      echo "skill-retire: cannot scan crew; preserving shared canonical" >&2
      return 0
    }
    self="$(cd -P "$ROOT" && pwd)"
    canonical="$(_kit_resolve_link "$SHARED_ROOT/$name" || true)"
    crew_db="$(_kit_resolve_link "$SHARED_HOME/crews/$KIT_NAME/$KIT_NAME.db" || true)"
    while IFS= read -r row; do
      [[ -n "$row" ]] || continue
      peer="${row#*:}"
      [[ "$(cd -P "$peer" 2>/dev/null && pwd)" != "$self" ]] || continue
      target="$(_kit_resolve_link "$peer/.claude/skills-bundle/$name" || true)"
      db="$(_kit_resolve_link "$peer/database/$KIT_NAME.db" || true)"
      if [[ -z "$canonical" || "$target" != "$canonical" ]]; then
        [[ -n "$crew_db" && -L "$peer/database/$KIT_NAME.db" && "$db" == "$crew_db" ]] || continue
      fi
      if owner="$(skill_retire_other_owner "$peer" "$name" "")"; then
        echo "skill-retire: $name: shared canonical retained for ${row%%:*}/$owner" >&2
        return 0
      fi
    done <<< "$scan"
    printf '%s' "$SHARED_ROOT"
  }

  # Run compat-lint (same gate as agent installs -- bypasses nothing).
  _COMPAT_LINT="$SCRIPT_DIR/compat-lint.sh"
  if [[ -f "$_COMPAT_LINT" ]]; then
    _FW_VERSION=""
    if [[ -f "$REPO_DIR/VERSION" ]]; then
      _FW_VERSION="$(tr -d '[:space:]' < "$REPO_DIR/VERSION")"
    fi
    if [[ -z "$_FW_VERSION" ]]; then
      _fail "$PACK_CLASS: compat-lint: cannot determine framework VERSION"
    fi
    _log "$PACK_CLASS: compat-lint: install-side gate (framework=$_FW_VERSION)"
    _lint_out=""
    if ! _lint_out="$(bash "$_COMPAT_LINT" \
        --pack-dir "$PACKAGE_DIR" \
        --framework-version "$_FW_VERSION" \
        --catalog "$CATALOG" \
        --install-side 2>&1)"; then
      while IFS= read -r _ll; do _log "  $_ll"; done <<< "$_lint_out"
      _fail "$PACK_CLASS: compat-lint install-side gate FAILED -- install aborted"
    fi
    while IFS= read -r _ll; do _log "  $_ll"; done <<< "$_lint_out"
    _log "$PACK_CLASS: compat-lint PASS"
  else
    # DGN-1682: under DOGANY_STRICT_GATES=1 (every update_apply.sh call) a
    # skipped gate is a nonzero exit, not a WARN -- the caller judges success
    # by rc + DOGANY_PACKS record + receipt, never by scanning this log.
    if [[ "${DOGANY_STRICT_GATES:-0}" == "1" ]]; then
      _fail "$PACK_CLASS: compat-lint.sh not found at $_COMPAT_LINT -- install-side gate cannot run under DOGANY_STRICT_GATES=1, refusing (fail-closed, DGN-1682)"
    fi
    _log "$PACK_CLASS: compat-lint WARN -- compat-lint.sh not found at $_COMPAT_LINT (gate skipped)"
  fi

  # ---- reverse-drift guard: kit_core (DGN-803 LS-4) ----
  # If the instance already has a <kit>.py with a HIGHER pin than the payload,
  # skip kit_core copy (loud SKIP, not an error). This is the install-side
  # equivalent of update.sh drift_guard_file (DGN-249 / §4 C2 live risk zero).
  KIT_CORE_SKIP=0
  if [[ "$PACK_CLASS" == "kit" ]]; then
  PAYLOAD_KIT_PY="$KIT_PAYLOAD_DIR/database/$KIT_NAME.py"
  INST_KIT_PY="$ROOT/database/$KIT_NAME.py"
  if [[ -f "$PAYLOAD_KIT_PY" && -f "$INST_KIT_PY" ]]; then
    _payload_pin="$(_kit_extract_ver_kit_py "$PAYLOAD_KIT_PY" 2>/dev/null || true)"
    _inst_pin="$(_kit_extract_ver_kit_py "$INST_KIT_PY" 2>/dev/null || true)"
    if [[ "$_payload_pin" =~ ^[0-9]+$ && "$_inst_pin" =~ ^[0-9]+$ ]]; then
      if [[ "$_inst_pin" -gt "$_payload_pin" ]]; then
        KIT_CORE_SKIP=1
        printf '%s\n' "============================================================"
        _log "kit: REVERSE-DRIFT GUARD: instance $KIT_NAME.py pin=$_inst_pin > payload pin=$_payload_pin -- kit_core copy SKIPPED (DGN-803 LS-4)"
        _log "kit: REVERSE-DRIFT GUARD: instance is ahead of payload; kit_core NOT overwritten (live risk zero, §4 C2)"
        printf '%s\n' "============================================================"
      elif [[ "$_inst_pin" -eq "$_payload_pin" ]]; then
        _log "kit: reverse-drift guard: pins equal (instance=$_inst_pin payload=$_payload_pin) -- kit_core PROCEED (idempotent)"
      else
        _log "kit: reverse-drift guard: payload pin=$_payload_pin > instance pin=$_inst_pin -- kit_core PROCEED (upgrade)"
      fi
    else
      # DGN-803 LS-5 hardening: parse failure was fail-open (PROCEED) in LS-4.
      # Fail-safe now: when both files exist but a pin cannot be parsed, the
      # guard cannot prove payload >= instance, so kit_core is conservatively
      # SKIPPED (loud). Parse-success cases above are unchanged.
      KIT_CORE_SKIP=1
      printf '%s\n' "============================================================"
      _log "kit: REVERSE-DRIFT GUARD: could not parse pin (payload='${_payload_pin:-}' instance='${_inst_pin:-}') -- kit_core copy SKIPPED (fail-safe, DGN-803 LS-5)"
      _log "kit: REVERSE-DRIFT GUARD: fix the unparseable EXPECTED_USER_VERSION line, then re-run the install"
      printf '%s\n' "============================================================"
    fi
  elif [[ -f "$PAYLOAD_KIT_PY" && ! -f "$INST_KIT_PY" ]]; then
    _log "kit: reverse-drift guard: instance has no $KIT_NAME.py -- fresh install, PROCEED"
  fi
  fi  # PACK_CLASS == kit (reverse-drift guard is a kit_core concern)

  # DGN-1423: a partial skip cannot count as a successful installation.
  [[ "$KIT_CORE_SKIP" -eq 0 ]] || _fail "pack admission: kit_core cannot be replaced safely; entire install refused"
  KIT_MIRROR_SKIP=0
  if [[ "$PACK_CLASS" == "kit" && -d "$KIT_PAYLOAD_DIR/mirror" ]]; then
    _km_payload_sb="$KIT_PAYLOAD_DIR/mirror/sdk_bridge.py"
    _km_inst_sb="$ROOT/mirror/sdk_bridge.py"
    if [[ -f "$_km_payload_sb" && -f "$_km_inst_sb" ]]; then
      _km_payload_pin="$(_kit_extract_ver_sdk_bridge_py "$_km_payload_sb" 2>/dev/null || true)"
      _km_inst_pin="$(_kit_extract_ver_sdk_bridge_py "$_km_inst_sb" 2>/dev/null || true)"
      if [[ "$_km_payload_pin" =~ ^[0-9]+$ && "$_km_inst_pin" =~ ^[0-9]+$ ]]; then
        if [[ "$_km_inst_pin" -gt "$_km_payload_pin" ]]; then
          KIT_MIRROR_SKIP=1
          printf '%s\n' "============================================================"
          _log "kit: REVERSE-DRIFT GUARD (mirror): instance sdk_bridge.py pin=$_km_inst_pin > payload pin=$_km_payload_pin -- kit_mirror copy SKIPPED (DGN-803/DGN-363)"
          _log "kit: REVERSE-DRIFT GUARD (mirror): instance is ahead of payload; mirror/ NOT overwritten"
          printf '%s\n' "============================================================"
        else
          _log "kit: reverse-drift guard (mirror): payload pin=$_km_payload_pin >= instance pin=$_km_inst_pin -- kit_mirror PROCEED"
        fi
      else
        KIT_MIRROR_SKIP=1
        printf '%s\n' "============================================================"
        _log "kit: REVERSE-DRIFT GUARD (mirror): could not parse sdk_bridge.py pin (payload='${_km_payload_pin:-}' instance='${_km_inst_pin:-}') -- kit_mirror copy SKIPPED (fail-safe, DGN-803/DGN-363)"
        _log "kit: REVERSE-DRIFT GUARD (mirror): fix the unparseable MIN_USER_VERSION line, then re-run the install"
        printf '%s\n' "============================================================"
      fi
    elif [[ -f "$_km_payload_sb" && ! -f "$_km_inst_sb" ]]; then
      _log "kit: reverse-drift guard (mirror): instance has no sdk_bridge.py -- fresh install, PROCEED"
    elif [[ ! -f "$_km_payload_sb" && -f "$_km_inst_sb" ]]; then
      # B2 (DGN-855): payload has no sdk_bridge.py but instance does -- the
      # payload is older or stripped; copying would silently clobber the instance's
      # ahead-version mirror files.  Treat as fail-safe SKIP (same as unparseable).
      KIT_MIRROR_SKIP=1
      printf '%s\n' "============================================================"
      _log "kit: REVERSE-DRIFT GUARD (mirror): payload has no sdk_bridge.py but instance does -- kit_mirror copy SKIPPED (fail-safe, DGN-855/DGN-803/DGN-363)"
      _log "kit: REVERSE-DRIFT GUARD (mirror): update the payload to include sdk_bridge.py before re-running the install"
      printf '%s\n' "============================================================"
    fi

    [[ "$KIT_MIRROR_SKIP" -eq 0 ]] || _fail "pack admission: kit_mirror cannot be replaced safely; entire install refused"
  fi

  # ---- content-drift guard (DGN-1109) ----
  # The reverse-drift guards above compare PINS ONLY (EXPECTED_USER_VERSION /
  # MIN_USER_VERSION). A live instance that drifted in CONTENT at the same
  # pin (live hotfixes, live-grown features) passes them as "PROCEED
  # (idempotent)" and is then silently truncated by the cp -f steps below
  # (observed 2026-08-26 on a live instance: lifekit.py live 14,792 lines vs tag payload
  # 13,058 at pin 34/34 -- 46 top-level symbols and 2,479 live-only lines
  # would have been erased; close_task.sh 9,662B -> 2,370B stub). And the
  # skills-bundle / routines-bundle / service surfaces had NO reverse-drift
  # guard at all. This guard compares the CONTENT of every file the copy
  # steps below would overwrite, before the first byte is written (and before
  # the dry-run exit, so an audit dry-run surfaces the same verdict).
  #
  # Signal, per file that exists on both sides with differing bytes:
  #   lost = lines present in live but absent from the payload (set
  #          difference -- a pure reorder/reformat counts 0; blank lines
  #          ignored). lost==0 (payload superset / pure addition) => safe.
  #   DESTRUCTIVE when lost > 0 AND any shrink signature holds:
  #     - top-level symbol loss (py ^def/^class, sh name())
  #     - net line shrink >= 5 (live lines - payload lines)
  #     - byte halving (live >= 1024B and payload < half of live)
  #   DRIFT (loud enumerate, PROCEED) when lost > 0 without any shrink
  #   signature -- small same-pin local edits do not brick reinstalls (a
  #   gate that blocks every normal reinstall gets bypassed by habit and is
  #   equivalent to silence, DGN-1005/1006), but what is about to be
  #   overwritten is named BEFORE the copy, never after.
  # Baseline (DGN-1491; semantics = DGN-1118, shared lib content_drift.py):
  #   the vendor payload tree of the version this instance CURRENTLY pins
  #   (DOGANY_PACKS <id>@<ver> -> tag pack/<id>/v<ver>, resolved through the
  #   update.sh ladder in lib/drift_baseline.sh, extracted read-only via
  #   `git archive`). live == baseline -> PASS outright: the live copy is
  #   untouched vendor content, so a payload that legitimately SHRINKS a file
  #   is vendor progression, not user loss (measured false-BLOCK without it:
  #   destructive=2/live_only=56 vs destructive=0 on the same payload with
  #   the baseline attached -- the main agent kit_life upgrade block). lost/symbol
  #   counts are NET OF BASELINE. This only ever REMOVES false positives:
  #   genuinely user-authored live content is in neither payload nor
  #   baseline, so its loss stays DESTRUCTIVE. Two distinct no-baseline
  #   states, both explicit in the log:
  #     - FRESH INSTALL (no DOGANY_PACKS pin for this id): no baseline EXISTS
  #       by definition; the strict DGN-1109 signal is the CORRECT signal
  #       (any pre-existing live file at a payload path has unknown
  #       provenance and must not be silently overwritten), not a fallback.
  #     - pin present but tag/repo unresolvable: loud NOTE + strict fallback
  #       (fail-closed direction -- strict blocks more, never less).
  # Verdict: any DESTRUCTIVE file => BLOCK (_fail, nothing written). Bypass
  #   requires DOGANY_PACK_ACCEPT_CONTENT_LOSS=<pack id> (exact id match);
  #   the accepted loss is still fully enumerated in the log as the receipt.
  # Scanned surfaces mirror the copy steps 1:1: kit_core database/ (skipped
  #   when KIT_CORE_SKIP=1 -- nothing will be copied there), kit_mirror
  #   mirror/ (same excludes, and only when the mirror pin guard below will
  #   let the copy proceed -- decision replicated in lockstep), own-mode
  #   skills-bundle, routines/bundle, service facade. share-mode skills land
  #   in the crew-shared root, not this instance -- NOT scanned (limitation).
  # -- baseline: vendor payload tree of the instance's currently-pinned tag --
  # (DGN-1491; mirrors update.sh content_drift_guard's baseline section.)
  _CD_BASE=""       # baseline PAYLOAD root ('' = judge with the strict signal)
  _CD_BASE_TMP=""   # mktemp extraction dir (removed right after the scan)
  # (|| true: a missing file makes sed rc=2, which under set -eo pipefail
  # would kill the whole install instead of judging "no pin".)
  _cd_prev_ver="$(sed -n 's/^DOGANY_PACKS=//p' "$ROOT/.instance.conf" 2>/dev/null | head -1 \
    | tr ',' '\n' | sed -n "s/^${PACK_ID}@//p" | head -1 || true)"
  if [[ -z "$_cd_prev_ver" ]]; then
    # Fresh install: no prior pin -> no baseline EXISTS by definition. The
    # strict DGN-1109 signal is the CORRECT signal here (not a degraded
    # fallback): a live file already sitting at a payload path has unknown
    # provenance, and overwriting it must stay loud/blocked.
    _log "$PACK_CLASS: content-drift guard: fresh install (no DOGANY_PACKS pin for $PACK_ID) -- no baseline exists by definition; strict DGN-1109 signal is the correct signal (DGN-1491)"
  else
    # Pack source repo: the payload's own work tree when it has one (the
    # direct/catalog lane -- the same repo the --source-tag check trusts,
    # see the SOURCE_TAG block above); otherwise the pack-sources registry
    # (the update_apply tag-archive extraction lane, which installs from a
    # .git-less temp tree extracted FROM that registry repo).
    _cd_bl_repo=""
    _cd_bl_sub=""
    if _cd_bl_repo="$(git -C "$PACKAGE_SRC_DIR" rev-parse --show-toplevel 2>/dev/null)" && [[ -n "$_cd_bl_repo" ]]; then
      # --show-toplevel is a PHYSICAL path; compare against the physical
      # PACKAGE_SRC_DIR (the work tree, not the DGN-1698 tag extraction; macOS /tmp -> /private/tmp) or the prefix strip misses.
      _cd_bl_sub="$(cd -P "$PACKAGE_SRC_DIR" 2>/dev/null && pwd || printf '%s' "$PACKAGE_SRC_DIR")"
      _cd_bl_sub="${_cd_bl_sub#"$_cd_bl_repo"}"
      _cd_bl_sub="${_cd_bl_sub#/}"
    else
      # (|| true: no registry file = "no repo known", never a set -e death.)
      _cd_bl_repo="$(sed -n "s/^${PACK_ID}\.repo=//p" \
        "${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}" 2>/dev/null | head -1 || true)"
    fi
    resolve_drift_baseline_tag "$_cd_bl_repo" "" "$_cd_prev_ver" "pack/$PACK_ID/v"
    if [[ -n "$DRIFT_BL_TAG" ]]; then
      _CD_BASE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dogany-cd-base.XXXXXX")" || _CD_BASE_TMP=""
      if [[ -n "$_CD_BASE_TMP" ]] \
         && git -C "$_cd_bl_repo" archive --format=tar "$DRIFT_BL_TAG" 2>/dev/null \
              | tar -xf - -C "$_CD_BASE_TMP" 2>/dev/null; then
        _CD_BASE="$_CD_BASE_TMP/${_cd_bl_sub:+$_cd_bl_sub/}$KIT_PAYLOAD_ROOT"
        if [[ -d "$_CD_BASE" ]]; then
          _log "$PACK_CLASS: content-drift guard: baseline = $DRIFT_BL_TAG (currently-pinned tag, read-only extraction; DGN-1491)"
        else
          _CD_BASE=""
          _log "$PACK_CLASS: content-drift guard NOTE: baseline tag $DRIFT_BL_TAG carries no payload root '$KIT_PAYLOAD_ROOT' -- judging with the strict DGN-1109 signal (a legitimate vendor shrink may block)"
        fi
      else
        [[ -n "$_CD_BASE_TMP" ]] && rm -rf "$_CD_BASE_TMP"
        _CD_BASE_TMP=""
        _log "$PACK_CLASS: content-drift guard NOTE: git archive $DRIFT_BL_TAG failed (repo=$_cd_bl_repo) -- judging with the strict DGN-1109 signal (a legitimate vendor shrink may block)"
      fi
    else
      _log "$PACK_CLASS: content-drift guard NOTE: pin $PACK_ID@$_cd_prev_ver but no baseline resolvable (repo=${_cd_bl_repo:-none}, reason=${DRIFT_BL_WHY:-none}) -- judging with the strict DGN-1109 signal (a legitimate vendor shrink may block)"
    fi
  fi

  _CD_ARGS=()
  _CD_PRESERVE=()   # DGN-1526 R2: --preserve flags (scanner only, not specs)
  _CD_TAB="$(printf '\t')"
  # _cd_spec SURFACE PAYLOAD LIVE -- append a spec, attaching the baseline
  # twin (4th field, DGN-1118 grammar) when the baseline payload tree carries
  # the same payload-relative path (update.sh _cd_spec parity, DGN-1491).
  _cd_spec() {
    local _surf="$1" _pay="$2" _live="$3" _basep="" _lrel _le
    # DGN-1526 R2: an owner entry is instance-root-relative; the scanner
    # matches SURFACE/REL. Translate each entry that reaches this spec's live
    # root (entry inside it -> SURFACE/<rest>; directory entry covering it ->
    # SURFACE/, the whole surface held). skills-bundle is the one surface
    # whose label differs from its instance path (.claude/skills-bundle).
    _lrel="${_live#"$ROOT"/}"
    for _le in ${LOCAL_LAYER_ENTRIES[@]+"${LOCAL_LAYER_ENTRIES[@]}"}; do
      if [[ "$_le" == "$_lrel/"* ]]; then
        _CD_PRESERVE[${#_CD_PRESERVE[@]}]="--preserve"
        _CD_PRESERVE[${#_CD_PRESERVE[@]}]="$_surf/${_le#"$_lrel/"}"
      elif [[ "$_le" == */ && "$_lrel/" == "$_le"* ]]; then
        _CD_PRESERVE[${#_CD_PRESERVE[@]}]="--preserve"
        _CD_PRESERVE[${#_CD_PRESERVE[@]}]="$_surf/"
      fi
    done
    if [[ -n "$_CD_BASE" ]]; then
      _basep="$_CD_BASE/${_pay#"$KIT_PAYLOAD_DIR"/}"
      [[ -e "$_basep" ]] || _basep=""
    fi
    if [[ -n "$_basep" ]]; then
      _CD_ARGS[${#_CD_ARGS[@]}]="${_surf}${_CD_TAB}${_pay}${_CD_TAB}${_live}${_CD_TAB}${_basep}"
    else
      _CD_ARGS[${#_CD_ARGS[@]}]="${_surf}${_CD_TAB}${_pay}${_CD_TAB}${_live}"
    fi
  }
  if [[ "$PACK_CLASS" == "kit" && "$KIT_CORE_SKIP" -eq 0 && -d "$KIT_PAYLOAD_DIR/database" ]]; then
    _cd_spec "database" "$KIT_PAYLOAD_DIR/database" "$ROOT/database"
  fi
  if [[ "$PACK_CLASS" == "kit" && -d "$KIT_PAYLOAD_DIR/mirror" ]]; then
    if [[ "$KIT_MIRROR_SKIP" -eq 0 ]]; then
      _cd_spec "mirror" "$KIT_PAYLOAD_DIR/mirror" "$ROOT/mirror"
    fi
  fi
  if [[ -d "$KIT_PAYLOAD_DIR/skills-bundle" ]]; then
    while IFS= read -r _cd_sd; do
      [[ -d "$_cd_sd" ]] || continue
      _cd_sn="$(basename "$_cd_sd")"
      if [[ "$(_kit_skill_mode "$_cd_sn")" == "share" ]]; then continue; fi
      _cd_spec "skills-bundle/$_cd_sn" "$_cd_sd" "$ROOT/.claude/skills-bundle/$_cd_sn"
    done < <(_selected_skill_dirs)
  fi
  if [[ -d "$KIT_PAYLOAD_DIR/routines/bundle" ]]; then
    _cd_spec "routines/bundle" "$KIT_PAYLOAD_DIR/routines/bundle" "$ROOT/routines/bundle"
  fi
  if [[ -n "$KNOW_WH" && -d "$KIT_PAYLOAD_DIR/knowledge/$KNOW_WH" ]]; then
    _cd_spec "knowledge/$KNOW_WH" "$KIT_PAYLOAD_DIR/knowledge/$KNOW_WH" "$ROOT/knowledge/$KNOW_WH"
  fi
  _cd_svc_ns="$KIT_NAME"
  if [[ "$PACK_CLASS" == "pack" ]]; then _cd_svc_ns="$SERVICE_NS"; fi
  if [[ -n "$_cd_svc_ns" && -d "$KIT_PAYLOAD_DIR/service/$_cd_svc_ns" ]]; then
    _cd_spec "service/$_cd_svc_ns" "$KIT_PAYLOAD_DIR/service/$_cd_svc_ns" "$ROOT/service/$_cd_svc_ns"
  fi
  # Scanner logic is SINGLE-SOURCED in scripts/pack/lib/content_drift.py
  # (DGN-1118 공용화): the same judgement also guards the update.sh lane, so
  # a second embedded copy here would be the DGN-1034 shape (two gates, one
  # contract). This lane passes dir specs plus the per-spec BASELINE field
  # when one resolved above (DGN-1491) -- still no subst / filter flags: pack
  # payloads land unrendered on these surfaces, so payload and baseline are
  # in the same render state and the DGN-1109 comparison stays byte-exact.
  _CD_LIB="${DOGANY_CONTENT_DRIFT_LIB:-$SCRIPT_DIR/lib/content_drift.py}"
  [[ -f "$_CD_LIB" ]] || _fail "$PACK_CLASS: CONTENT-DRIFT GUARD (DGN-1109): scanner lib missing ($_CD_LIB) -- torn tree, cannot prove the copy is safe, fail-closed (DGN-1004)"
  _cd_rc=0
  _cd_out="$("$PACK_PY3" "$_CD_LIB" ${_CD_PRESERVE[@]+"${_CD_PRESERVE[@]}"} ${_CD_ARGS[@]+"${_CD_ARGS[@]}"})" || _cd_rc=$?
  # Baseline extraction consumed by the scan above -- drop it before the
  # verdict branches (the BLOCK branch _fails without returning here).
  if [[ -n "$_CD_BASE_TMP" ]]; then rm -rf "$_CD_BASE_TMP"; _CD_BASE_TMP=""; fi
  if [[ "$_cd_rc" -eq 4 ]]; then
    printf '%s\n' "============================================================"
    _log "$PACK_CLASS: CONTENT-DRIFT GUARD (DGN-1109): pins allow the copy, but the payload would ERASE live content:"
    while IFS= read -r _cd_l; do _log "  $_cd_l"; done <<< "$_cd_out"
    if [[ "${DOGANY_PACK_ACCEPT_CONTENT_LOSS:-}" == "$PACK_ID" ]]; then
      # DGN-1428: operator acceptance is conditional on durable evidence.
      # Dry runs never claim that preservation has already succeeded.
      if [[ "$DRY" -eq 1 ]]; then
        _log "$PACK_CLASS: CONTENT-DRIFT GUARD: dry-run; accepted loss requires preservation during apply"
      else
        _cd_archive="$("$PACK_PY3" "$SCRIPT_DIR/lib/preserve_content_loss.py" \
          "$ROOT" "$PACK_ID" "$RECORD_VERSION" ${_CD_ARGS[@]+"${_CD_ARGS[@]}"} <<< "$_cd_out")" \
          || _fail "$PACK_CLASS: CONTENT-DRIFT GUARD: preservation failed; loss acceptance refused"
        _log "$PACK_CLASS: CONTENT-DRIFT GUARD: loss ACCEPTED by operator; originals and verdict preserved at $_cd_archive"
      fi
      printf '%s\n' "============================================================"
    else
      _log "$PACK_CLASS: CONTENT-DRIFT GUARD: back-port the live-only content into the payload (3-way), or re-run with DOGANY_PACK_ACCEPT_CONTENT_LOSS=$PACK_ID to accept the enumerated loss"
      printf '%s\n' "============================================================"
      _fail "$PACK_CLASS: CONTENT-DRIFT GUARD BLOCK (DGN-1109): destructive overwrite detected -- nothing was written"
    fi
  elif [[ "$_cd_rc" -eq 3 ]]; then
    _log "$PACK_CLASS: content-drift guard (DGN-1109): non-destructive drift -- these live-only lines WILL be overwritten:"
    while IFS= read -r _cd_l; do _log "  $_cd_l"; done <<< "$_cd_out"
    _log "$PACK_CLASS: content-drift guard: PROCEED (no shrink signature: no symbol loss, net shrink < 5 lines, no byte halving)"
  elif [[ "$_cd_rc" -eq 0 ]]; then
    _log "$PACK_CLASS: content-drift guard (DGN-1109): PASS -- no live-only content would be overwritten"
  else
    _fail "$PACK_CLASS: CONTENT-DRIFT GUARD (DGN-1109): scanner failed (rc=$_cd_rc) -- cannot prove the copy is safe, fail-closed (DGN-1004)"
  fi

  # DGN-1436 #3: crew lockstep, checked while the instance is still untouched.
  # Placed immediately before the DRY branch on purpose -- that branch exits
  # advertising "no writes", which is the standing proof that nothing above
  # this line has mutated the instance. Runs in BOTH modes (it used to live
  # inside the DRY branch only; see _kit_crew_lockstep_preflight's header).
  _kit_crew_lockstep_preflight

  # DGN-1682 item 4 (join half): a FIRST-TIME join of an existing crew must
  # land the version the crew runs. The auto-resolve path above already
  # picks that version; this covers the direct/pipeline kit install (and is
  # a second belt for the auto-resolve sub-install). Existing members
  # (database/<kit>.db present) are not judged here: a crew update moves
  # them one by one inside update_apply.sh's transaction, and a lagging
  # member's own kit --upgrade is the documented way to re-align a split.
  # Same position as the lockstep preflight: before the DRY branch, before
  # any write, identical verdict in both modes.
  if [[ "$PACK_CLASS" == "kit" && "$KIT_DB_LANE" -eq 1 \
        && ! -e "$ROOT/database/$KIT_NAME.db" && ! -L "$ROOT/database/$KIT_NAME.db" ]]; then
    _crew_kit_version "$KIT_NAME" \
      || _fail "kit: crew kit version: HOME estate scan failed -- cannot read which '$KIT_NAME' version the existing crew runs, refusing the join before any change, fail-closed (DGN-1682)"
    case "$CREW_KIT_STATE" in
      none) : ;;
      split)
        _fail "kit: crew kit version: the existing '$KIT_NAME' crew is already split across versions ($CREW_KIT_PEERS) -- joining at $RECORD_VERSION would widen it; bring the crew to ONE kit version first. Refused before any change (DGN-1682)" ;;
      unrecorded)
        _fail "kit: crew kit version: a '$KIT_NAME' crew member mounts the shared DB with no '$KIT_NAME@' DOGANY_PACKS record ($CREW_KIT_PEERS) -- the crew's version cannot be read, refusing the join before any change, fail-closed (DGN-1682)" ;;
      uniform)
        if [[ "$RECORD_VERSION" == "$CREW_KIT_V" ]]; then
          _log "kit: crew kit version: joining the '$KIT_NAME' crew at $RECORD_VERSION == crew version ($CREW_KIT_PEERS) -- coherent (DGN-1682)"
        elif [[ "$("$PACK_PY3" "$SCRIPT_DIR/lib/semver_range.py" compare "$RECORD_VERSION" "$CREW_KIT_V" 2>/dev/null)" == "1" ]]; then
          _gate_stop kit.crew-version "kit: crew kit version: this install would join the '$KIT_NAME' crew at $RECORD_VERSION while the crew runs $CREW_KIT_V ($CREW_KIT_PEERS) -- that splits the crew (update_plan.sh then ABORTs every update). Upgrade the crew's kit first (raise an update offer for $RECORD_VERSION; owner picks it in /update), then join. Refused before any change (DGN-1682)"
        else
          _fail "kit: crew kit version: this install would join the '$KIT_NAME' crew at $RECORD_VERSION while the crew runs $CREW_KIT_V ($CREW_KIT_PEERS) -- install the crew's version ($CREW_KIT_V, --source-tag pack/$KIT_NAME/v$CREW_KIT_V) instead. Refused before any change (DGN-1682)"
        fi ;;
    esac
  fi

  if [[ "$DRY" -eq 1 ]]; then
    _log "dry-run: $PACK_CLASS install plan for $PACK_ID -> $ROOT"
    _log "dry-run: payload=$KIT_PAYLOAD_DIR"
    if [[ "$PACK_CLASS" == "kit" ]]; then
    _log "dry-run: kit_core_skip=$KIT_CORE_SKIP"
    _log "dry-run: provides_kit=$KIT_PROVIDES pack_version=$PACK_VERSION"
    _log "dry-run: kit_name=$KIT_NAME db_lane=$KIT_DB_LANE schedules=$KIT_SCHEDULES completion_verb='${KIT_COMPLETION_VERB:-<none>}'"
    if [[ "$KIT_SCHEDULES" -eq 1 ]]; then
      while IFS= read -r _sd_f; do
        [[ -e "$_sd_f" ]] || continue
        _log "dry-run: routines-schedules: would render+deliver routines/$(basename "$_sd_f") (capabilities.schedules=true, DGN-1402)"
      done < <(find "$KIT_PAYLOAD_DIR/routines" -maxdepth 1 -type f -name '*.plist' | sort)
      if [[ -f "$KIT_PAYLOAD_DIR/routines/plists.defer" ]]; then
        _log "dry-run: routines-schedules: would merge-append payload/routines/plists.defer into $ROOT/routines/plists.defer"
      fi
    fi
    # completion predicate (DGN-1374): judgment only, same marker path the
    # real run reads -- no rm, no write. Same code path, no side effect.
    if [[ -n "$KIT_COMPLETION_VERB" ]]; then
      _dr_marker="$ROOT/config/packs/$PACK_ID.complete"
      if [[ -f "$_dr_marker" ]]; then
        _log "dry-run: completion: verb='$KIT_COMPLETION_VERB' marker EXISTS ($_dr_marker) -- would report COMPLETE unless this run lands new kit_core payload (which invalidates it)"
      else
        _log "dry-run: completion: verb='$KIT_COMPLETION_VERB' marker ABSENT ($_dr_marker) -- would report INSTALLED, NOT COMPLETE"
      fi
    else
      _log "dry-run: completion: no completion_verb declared -- installed == complete"
    fi
    # DGN-1436 crew-lockstep: no longer previewed here. This block was the
    # ONLY caller that ran the guard before anything was written, which made
    # the pre-mutation refusal a --dry-run privilege -- exactly the direct-
    # call-path hole DGN-1436 #3 names. The identical check now runs for both
    # modes at _kit_crew_lockstep_preflight (called just above this branch),
    # so dry-run keeps the same verdict and the same zero writes. Do not
    # re-add a dry-run-only copy: two call sites is how the real path lost it.
    else
    _log "dry-run: behavior-pack profile (DGN-1018): kit_core/db-init/migrate/mirror NOT executed"
    _log "dry-run: service_namespace='${SERVICE_NS:-}' pack_version=$PACK_VERSION"
    if [[ -n "$KNOW_WH" ]]; then
      _log "dry-run: knowledge_warehouse='$KNOW_WH' action=install-plan (would copy payload/knowledge/$KNOW_WH/ -> $ROOT/knowledge/$KNOW_WH/, DGN-1426)"
    else
      _log "dry-run: knowledge_warehouse=(none declared) -- knowledge/ would NOT be installed"
    fi
    fi
    if [[ -f "$KIT_PAYLOAD_DIR/requirements.txt" ]]; then
      _log "dry-run: deps-provision: payload/requirements.txt present -> would pip-provision runtime interpreters (DGN-850)"
    else
      _log "dry-run: deps-provision: no payload/requirements.txt -> no-op"
    fi
    if [[ -f "$KIT_PAYLOAD_DIR/config/outbound-allowlist" ]]; then
      _log "dry-run: outbound-allowlist: would install config/packs/$PACK_ID.outbound-allowlist (DGN-1860)"
    elif [[ -f "$ROOT/config/packs/$PACK_ID.outbound-allowlist" ]]; then
      _log "dry-run: outbound-allowlist: payload ships none -> would remove config/packs/$PACK_ID.outbound-allowlist (DGN-1860)"
    fi
    # DGN-1496: declared hook wiring plan -- validation runs here too, so a
    # broken declaration fails the dry run instead of the real install.
    if _hooks_declared "$MANIFEST"; then
      _hooks_validate "dry-run: hook-wiring" "$MANIFEST" "$KIT_PAYLOAD_DIR" bundle
      while IFS=$'\t' read -r _dr_ev _dr_sc _dr_to; do
        [[ -n "$_dr_ev" ]] || continue
        _log "dry-run: hook-wiring: would wire $_dr_ev -> $ROOT/$_dr_sc (timeout ${_dr_to}s, idempotent)"
      done <<< "$_HOOK_LINES"
    else
      _log "dry-run: hook-wiring: no manifest hooks[] -> no-op"
    fi
    # DGN-956: per-skill sharing plan (mode + intended action, no writes).
    if [[ -d "$KIT_PAYLOAD_DIR/skills-bundle" ]]; then
      while IFS= read -r _skill_dir; do
        [[ -d "$_skill_dir" ]] || continue
        _sn="$(basename "$_skill_dir")"
        if [[ "$(_kit_skill_mode "$_sn")" == "own" ]]; then
          _log "dry-run: skills-bundle: $_sn mode=own action=copy"
        elif [[ "$PACK_CLASS" == "pack" ]]; then
          _log "dry-run: skills-bundle: $_sn mode=share action=BLOCK (share is undefined for class=pack -- no kit-keyed shared root; DGN-1018)"
        else
          _site="$ROOT/.claude/skills-bundle/$_sn"
          _act="link"
          if [[ -L "$_site" ]]; then
            if [[ "$(readlink "$_site")" == "$SHARED_ROOT/$_sn" ]]; then _act="link-ok"; else _act="relink"; fi
          elif [[ -d "$_site" ]]; then
            _act="convert-or-fail(F4)"
          fi
          _log "dry-run: skills-bundle: $_sn mode=share action=$_act target=$SHARED_ROOT/$_sn"
        fi
        # DGN-1409: activation link plan (.claude/skills/<id> -> skills-bundle/<id>),
        # same for own and share -- see the real step for the byte-identical logic.
        _alink="$ROOT/.claude/skills/$_sn"
        if ! _skill_is_active "$_sn"; then
          _log "dry-run: skills-bundle: $_sn activation-link=none (delivered, not selected; existing link left as is)"
        elif [[ -L "$_alink" && "$(readlink "$_alink")" == "../skills-bundle/$_sn" ]]; then
          _log "dry-run: skills-bundle: $_sn activation-link=ok (.claude/skills/$_sn)"
        else
          _log "dry-run: skills-bundle: $_sn activation-link=(re)link (.claude/skills/$_sn -> ../skills-bundle/$_sn)"
        fi
      done < <(_selected_skill_dirs)
    fi
    # DGN-1422: declared-vs-real skill retire plan (the DGN-1409 counterpart).
    # NEW declared set = the delivered payload skills (empty when the
    # category is absent -- a manifest that drops the category entirely
    # retires everything this pack previously declared, same as dropping
    # every individual name). No writes: skill_retire_plan only inspects.
    _skills_declared_file="$ROOT/config/packs/$PACK_ID.skills"
    _retired_preview="$(skill_retire_diff "$_skills_declared_file" "$_new_skill_names")"
    if [[ -n "$_retired_preview" ]]; then
      while IFS= read -r _rn; do
        [[ -n "$_rn" ]] || continue
        if _rn_owner="$(skill_retire_other_owner "$ROOT" "$_rn" "$PACK_ID")"; then
          _log "dry-run: skill-retire: $_rn: would SKIP archive -- still declared by $_rn_owner"
          continue
        fi
        while IFS= read -r _rl; do _log "dry-run: $_rl"; done < <(skill_retire_plan "$ROOT" "$(_retire_shared_root "$_rn")" "$_rn")
      done <<< "$_retired_preview"
    else
      _log "dry-run: skill-retire: no skills dropped from this pack's declaration -- 0 would be removed"
    fi
    echo "[pack_install] DRY-RUN OK ($PACK_CLASS) -- no writes"
    exit 0
  fi

  # ---- NM3: payload checksum verification GATE (DGN-227 B4-5 / D2) ----
  # packs/CONTRACT.md §5-1 step 5, in the documented order: it runs after the
  # requires_kit gate (1), the inline re-validation (2), the compat-lint
  # install-side gate (3) and the reverse-drift guard (4), and BEFORE the
  # first byte of payload is written. Placed after the dry-run exit for the
  # same reason the legacy path places it there: NM3 is an apply-time
  # integrity gate, and a dry-run writes nothing to protect.
  # DGN-1549: the payload root scopes the unsealed-extras scan on the
  # work-tree lane (repo-side debris outside payload/ never installs and
  # must not false-refuse -- measured: dogany-lifekit carries ignored
  # .bak-quarantine/ at its root with a clean payload/).
  _nm3_verify "$PACKAGE_SRC_DIR" "$KIT_PAYLOAD_ROOT"

  # ---- DGN-1460: install evidence -- in-progress marker BEFORE the first
  # payload byte. The expected-anchor list is fixed HERE, from the payload's
  # own declarations, so a delivery step that never runs still leaves its
  # anchor unmet (a list of "copies that happened to succeed" would pass
  # vacuously). No cleanup trap on purpose: surviving a mid-delivery kill is
  # what makes the marker evidence -- readers see INCOMPLETE, never
  # "installed", until evidence_complete below promotes it.
  _ev_anchors=""
  while IFS= read -r _ev_name; do
    [[ -n "$_ev_name" ]] || continue
    # Keep every selected name even if the source disappears before delivery:
    # a missing copy must leave an unmet anchor, never shrink the receipt.
    # Anchors = activation links of the ACTIVE set only (delivered-only
    # bodies are inert until the owner switches them on).
    _ev_anchors+=".claude/skills/$_ev_name"$'\n'
  done <<< "$_active_skill_names"
  if [[ -n "${KNOW_WH:-}" && -d "$KIT_PAYLOAD_DIR/knowledge/$KNOW_WH" ]]; then
    _ev_anchors+="knowledge/$KNOW_WH"$'\n'
  fi
  _ev_svc_ns="$KIT_NAME"
  [[ "$PACK_CLASS" == "pack" ]] && _ev_svc_ns="${SERVICE_NS:-}"
  if [[ -n "$_ev_svc_ns" && -d "$KIT_PAYLOAD_DIR/service/$_ev_svc_ns" ]]; then
    _ev_anchors+="service/$_ev_svc_ns"$'\n'
  fi
  # Captured BEFORE this run writes its own marker: did a PRIOR run of this
  # same pack leave install evidence here? Consumed by the service-conflict
  # gate below to distinguish "own interrupted install's residue" from
  # "another pack's surface" -- checking after evidence_begin would always
  # be true (this run's own marker) and void that gate.
  _EV_PRIOR_OWN=0
  for _ev_f in "$ROOT/config/packs/$PACK_ID.receipt.inprogress" "$ROOT/config/packs/$PACK_ID.receipt"; do
    if [[ -f "$_ev_f" ]] && grep -qx "pack=$PACK_ID" "$_ev_f" 2>/dev/null; then
      _EV_PRIOR_OWN=1
    fi
  done
  evidence_begin "$ROOT" "$PACK_ID" "$RECORD_VERSION" "$PACK_CLASS" "$_ev_anchors" "$EVIDENCE_SOURCE" \
    || _fail "install evidence: in-progress receipt write failed -- refusing to mutate the instance without a durable run record (DGN-1460)"
  _log "  install evidence: in-progress marker issued (config/packs/$PACK_ID.receipt.inprogress, $(printf '%s' "$_ev_anchors" | grep -c . | tr -d ' ') expected anchor(s))"

  if [[ "$PACK_CLASS" == "pack" ]]; then
    # ---- behavior-pack profile (DGN-1018 §5-B): kit-exclusive steps OFF ----
    _log "pack: kit_core/db-init/migrate: kit-exclusive steps -- NOT executed (behavior-pack profile)"
  else
  # ---- kit_core: payload/database/* -> instance/database/ ----
  if [[ "$KIT_CORE_SKIP" -eq 0 ]]; then
    if [[ -d "$KIT_PAYLOAD_DIR/database" ]]; then
      _log "kit: step kit_core: database/ copy"
      mkdir -p "$ROOT/database"
      # Copy each file/directory inside payload/database/ preserving layout.
      # rsync-style: only copy files present in payload (no --delete; C1 invariant).
      while IFS= read -r _src; do
        [[ -e "$_src" ]] || continue
        [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
        # Relative path from payload/database
        _rel="${_src#"$KIT_PAYLOAD_DIR/database/"}"
        _ct_deliver "$_src" "database/$_rel" || continue
        _log "  kit_core: $_rel"
      done < <(find "$KIT_PAYLOAD_DIR/database" -type f)
      _log "kit: kit_core done"
    else
      _log "kit: kit_core: no payload/database/ found -- skipping"
    fi
  else
    _log "kit: kit_core SKIPPED (reverse-drift guard: instance pin ahead of payload)"
  fi

  # ---- structured lane init: schema.sql -> <kit>.db (DGN-803 LS-5) ----
  # Moved here from mint.sh step 5 (grill G2): the framework no longer ships
  # schema.sql, so the empty structured lane is created at KIT ACTIVATION,
  # from the schema the kit_core step just installed. Contract preserved from
  # mint.sh: keep-if-exists (idempotent; an upgrade/reinstall NEVER clobbers
  # user data -- migrations own version moves), and sqlite3 absence is a HARD
  # FAIL (DGN-674 F7: a silent skip shipped instances whose structured lane
  # never existed and the kit broke much later with no breadcrumb).
  # DGN-1003: DB filename derives from KIT_NAME (database/<kit>.db); a kit
  # declaring capabilities.db_lane=false skips db-init AND migrate cleanly
  # (no DB lane to initialize -- matching the compat-lint C3/C6 capability
  # skip, so gate verdict and installer behavior agree).
  # DGN-1436: the structured lane is crew-shared, not instance-local (see
  # _kit_crew_db_link_ensure above for the full case analysis) -- db-init
  # delegates to that function rather than creating an instance-local file
  # directly. -L is checked BEFORE -f (which follows the link): the old
  # -f-only gate treated a dangling symlink as "absent" and let `mv` silently
  # replace a broken/misdirected crew link with a fresh unrelated real file;
  # explicit -L handling inside _kit_crew_db_link_ensure closes that.
  # DGN-1869: the installer owns both physical join and registration.
  # Consent is still required to join an existing crew. Registration runs
  # after the mount ledger is written, including for the first member.
  _kit_crew_join_gate() {
    local crew_db="$SHARED_HOME/crews/$KIT_NAME/$KIT_NAME.db"
    if [[ ! -e "$_KIT_DB" || -L "$_KIT_DB" ]]; then
      if ! grep -qxF "member=$ROOT" "$CREW_DIR/$KIT_NAME/crew.conf" 2>/dev/null; then
        [[ ! -e "$CREW_DIR/$KIT_NAME/.lease" && ! -e "$CREW_DIR/$KIT_NAME/crew.declared" ]] \
          || _fail "kit: crew registration refused while lease or declaration exists"
      fi
    fi
    if [[ -e "$_KIT_DB" || -L "$_KIT_DB" ]]; then
      return 0
    fi
    if [[ ! -e "$crew_db" ]]; then
      return 0
    fi
    if [[ "${DOGANY_CREW_JOIN_ACK:-0}" == "1" ]]; then
      _log "kit: db-init: crew join ACK (DOGANY_CREW_JOIN_ACK=1)"
      return 0
    fi
    _fail "kit: db-init: REFUSING direct crew join -- '$KIT_NAME' already has a crew-shared DB ($crew_db) with an existing member, and this is a first-time join for this instance via the direct install path (not a crew-scoped update_apply.sh transaction)."
  }

  _KIT_SCHEMA="$ROOT/database/schema.sql"
  _KIT_DB="$ROOT/database/$KIT_NAME.db"
  if [[ "$KIT_DB_LANE" -eq 0 ]]; then
    _log "kit: db-init: capabilities.db_lane=false -- SKIP (kit declares no DB lane)"
  else
    _kit_crew_join_gate
    _kit_crew_db_link_ensure
  fi

  # ---- kit-migrate: forward-only schema migrations (DGN-783 B1) ----
  # Single-owner seam: the kit install step owns <kit>.db migrations.
  # (update.sh 3f consumes $REPO_ROOT/database/migrations, which the
  # framework repo no longer ships since LS-5 -- the migration channel rides
  # the kit payload, landed by the kit_core copy above.)
  # Forward-only: apply instance database/migrations/NNN_*.sql with
  # cur_ver < NNN <= code pin, ascending, backing up the DB before each
  # apply. Bounded at the pin so forked-lineage files above the pin are
  # never pulled in. After the pass the DB MUST sit exactly at the pin --
  # a gap (pin ahead with no migration file) is FATAL: exiting 0 while the
  # payload runtime fail-closes on user_version was the B1 defect.
  _KIT_MIG_DIR="$ROOT/database/migrations"
  _KIT_INST_PY="$ROOT/database/$KIT_NAME.py"
  if [[ "$KIT_DB_LANE" -eq 0 ]]; then
    _log "kit: migrate: capabilities.db_lane=false -- SKIP (kit declares no DB lane)"
  elif [[ ! -f "$_KIT_INST_PY" ]]; then
    _log "kit: migrate: no instance database/$KIT_NAME.py -- no versioned kit core, skipping"
  elif [[ ! -f "$_KIT_DB" ]]; then
    _log "kit: migrate: no $KIT_NAME.db -- nothing to migrate (db-init owns creation)"
  else
    command -v sqlite3 >/dev/null 2>&1 \
      || _fail "kit: migrate: sqlite3 not found -- cannot verify/advance $KIT_NAME.db schema (fail-closed)"
    _target_pin="$(_kit_extract_ver_kit_py "$_KIT_INST_PY" 2>/dev/null || true)"
    if [[ ! "$_target_pin" =~ ^[0-9]+$ ]]; then
      if [[ "$KIT_CORE_SKIP" -eq 1 ]]; then
        # Pre-existing unparseable pin: this install changed neither code nor
        # DB (reverse-drift guard already screamed and skipped kit_core).
        # Loud skip keeps LS-5 semantics -- no NEW breakage was introduced.
        _log "kit: migrate: instance $KIT_NAME.py pin unparseable and kit_core was SKIPPED -- migrate skipped (state unchanged)"
      else
        _fail "kit: migrate: cannot parse EXPECTED_USER_VERSION from freshly installed $KIT_NAME.py -- DB/code sync unverifiable"
      fi
    else
      _cur_ver="$(sqlite3 "$_KIT_DB" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
      _cur_ver="${_cur_ver:-0}"
      # DGN-1522: preflight -- reject migration-number collisions before
      # applying anything. The filter below picks by filename NUMBER only;
      # two files sharing a number are BOTH applied (glob is alphabetical)
      # and _cur_ver is a single loop-entry read never refreshed mid-loop,
      # so a collision can stamp user_version BACKWARDS before the second
      # file's DDL re-runs and hard-fails. Fail closed here instead of
      # mid-apply (bash 3.2 target -- no associative arrays, sort/uniq -d).
      _mig_dupes="$(for _f in "$_KIT_MIG_DIR"/[0-9][0-9][0-9]_*.sql; do
        [[ -e "$_f" ]] || continue
        basename "$_f" | cut -d_ -f1
      done | LC_ALL=C sort | uniq -d)"
      if [[ -n "$_mig_dupes" ]]; then
        _fail "kit: migrate: migration number collision(s) in $_KIT_MIG_DIR -- refusing (fail-closed; ambiguous which file is canonical, DGN-1522): $(printf '%s' "$_mig_dupes" | tr '\n' ' ')"
      fi
      if [[ "$_cur_ver" -gt "$_target_pin" ]]; then
        _fail "kit: migrate: DB user_version=$_cur_ver is AHEAD of code pin=$_target_pin -- refusing (forward-only; reconcile the migration lineage first)"
      fi
      if [[ "$_cur_ver" -lt "$_target_pin" ]]; then
        # DGN-1436: about to advance a potentially crew-shared DB -- refuse
        # if any crew member's code isn't pinned to the version we're about
        # to stamp (see _kit_crew_lockstep_guard header for the full case).
        _kit_crew_lockstep_guard "$_target_pin"
        _log "kit: migrate: DB user_version=$_cur_ver < code pin=$_target_pin -- applying forward migrations"
        for _mig in "$_KIT_MIG_DIR"/[0-9][0-9][0-9]_*.sql; do
          [[ -e "$_mig" ]] || continue
          _mbase="$(basename "$_mig")"
          _mnnn="${_mbase%%_*}"
          _mn=$((10#$_mnnn))
          [[ "$_mn" -gt "$_cur_ver" && "$_mn" -le "$_target_pin" ]] || continue
          # Back up BEFORE each apply. WAL-safe .backup + version-stamped
          # filename per the DGN-672 C3 snapshot contract (same shape as
          # update.sh 3f produced; restore-data.sh --list surfaces these).
          _mts="$(date +%Y%m%d-%H%M%S)"
          _mv_now="$(sqlite3 "$_KIT_DB" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
          _mbak="$ROOT/database/$KIT_NAME.db.v${_mv_now}.bak-$_mts"
          sqlite3 "$_KIT_DB" ".backup '$_mbak'" \
            || _fail "kit: migrate: failed to back up $KIT_NAME.db before migration $_mnnn"
          _log "  migrate: backup -> $_mbak"
          # -bail (DGN-1032): without it sqlite3 keeps executing past a
          # failing statement, so a mid-file failure still reaches the
          # trailing PRAGMA user_version=<target> + COMMIT and stamps/commits
          # the target version over a half-applied schema. On RE-RUN
          # cur_ver==target_pin then skips the apply loop entirely and the
          # end-check below only compares version numbers -> the partial
          # schema reads as "in sync" forever. -bail aborts at the FIRST
          # failing statement, before the version stamp is ever reached, so
          # the DB is left below target and the apply loop retries next run.
          sqlite3 -bail "$_KIT_DB" < "$_mig" \
            || _fail "kit: migrate: migration $_mnnn failed ($_mbase); DB backup at $_mbak"
          _log "  migrate: applied $_mnnn ($_mbase)"
        done
      fi
      _end_ver="$(sqlite3 "$_KIT_DB" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
      if [[ "$_end_ver" -ne "$_target_pin" ]]; then
        _fail "kit: migrate: DB user_version=$_end_ver != code pin=$_target_pin after forward pass -- missing migration file(s); install FAILED (no exit-0 with a fail-closed runtime)"
      fi
      _log "kit: migrate: DB user_version=$_end_ver == code pin=$_target_pin (in sync)"
    fi
  fi
  fi  # PACK_CLASS == kit (kit_core / db-init / migrate)

  # ---- completion predicate: installed != complete (DGN-1374) ---------------
  # "complete" is a DECLARED fact, never inferred from row counts / NULLs --
  # that inference (reading a shadow-measure column to guess sync state) is
  # the exact failure mode this ticket exists to close. The marker file is
  # owned by the pack's own completion_verb (it writes the marker when that
  # verb finishes) -- the installer only reads it and reports; it never runs
  # the verb itself (out of scope: an installer that calls arbitrary
  # data-mutating pack verbs is the next incident, not this ticket's fix).
  #
  # kit_core landing new/updated payload this run invalidates any existing
  # marker: the code the data was last reconciled against is now superseded,
  # so completeness must be re-earned. A kit that declares no completion_verb
  # keeps today's behavior unchanged (installed == complete).
  if [[ "$PACK_CLASS" == "kit" && -n "$KIT_COMPLETION_VERB" ]]; then
    _kit_complete_marker="$ROOT/config/packs/$PACK_ID.complete"
    if [[ "$KIT_CORE_SKIP" -eq 0 && -f "$_kit_complete_marker" ]]; then
      rm -f "$_kit_complete_marker"
      _log "kit: completion: prior marker invalidated -- kit_core landed payload this run, re-confirmation required"
    fi
    if [[ -f "$_kit_complete_marker" ]]; then
      KIT_COMPLETE=1
      _log "kit: completion: $PACK_ID COMPLETE (completion_verb='$KIT_COMPLETION_VERB' confirmed via $_kit_complete_marker)"
    else
      KIT_COMPLETE=0
      _log "kit: completion: $PACK_ID INSTALLED, NOT COMPLETE -- pending data-dependent step: '$KIT_COMPLETION_VERB' (run it manually; the installer does not run it for you). marker checked: $_kit_complete_marker"
    fi
  fi

  # ---- service_facade: payload/service/<ns>/* -> instance/service/<ns>/ ----
  # kit (DGN-1003): <ns> = KIT_NAME (compat-lint C4 allows exactly
  # service/<kit>/ in the payload -- lockstep).
  # pack (DGN-1018 §5-B): <ns> = SERVICE_NS (manifest service_namespace),
  # same copy machinery. CONFLICT BLOCK on FIRST install: when
  # $ROOT/service/<ns> already exists but this pack id has NO DOGANY_PACKS
  # entry, the target surface belongs to some OTHER pack/kit -- overwriting
  # it is refused. A re-install/upgrade (own id already recorded) proceeds.
  _SVC_NS="$KIT_NAME"
  if [[ "$PACK_CLASS" == "pack" ]]; then
    _SVC_NS="$SERVICE_NS"
  fi
  if [[ -n "$_SVC_NS" && -d "$KIT_PAYLOAD_DIR/service/$_SVC_NS" ]]; then
    if [[ "$PACK_CLASS" == "pack" && -e "$ROOT/service/$_SVC_NS" ]]; then
      _self_recorded="$("$PACK_PY3" - "$ROOT/.instance.conf" "$PACK_ID" <<'PYEOF'
import sys, pathlib
conf = pathlib.Path(sys.argv[1])
pid = sys.argv[2]
found = 0
if conf.exists():
    try:
        for ln in conf.read_text(encoding="utf-8").splitlines():
            if ln.startswith("DOGANY_PACKS="):
                items = [x for x in ln.split("=", 1)[1].split(",") if x]
                if any(x.split("@", 1)[0] == pid for x in items):
                    found = 1
                break
    except Exception:
        found = 0
print(found)
PYEOF
)" || _self_recorded=0
      if [[ "$_self_recorded" != "1" ]]; then
        # DGN-1456/1460: an INTERRUPTED install of this same pack delivers
        # the service surface but never reaches the ledger upsert -- the old
        # ledger-only probe then read the rerun (the sanctioned repair path)
        # as "another pack's surface" and blocked it. _EV_PRIOR_OWN was
        # captured BEFORE this run's own evidence_begin (checking now would
        # always see this run's marker and void the gate): a prior own-id
        # marker/receipt proves the surface is this pack's own residue, not
        # someone else's. A foreign surface still blocks.
        if [[ "${_EV_PRIOR_OWN:-0}" -eq 1 ]]; then
          _log "pack: service_facade: $ROOT/service/$_SVC_NS exists with NO ledger entry, but prior own-id install evidence found -- interrupted own install, re-delivery proceeds (DGN-1460 repair lane)"
        else
          _fail "pack: service conflict BLOCK: $ROOT/service/$_SVC_NS already exists and pack id '$PACK_ID' has no DOGANY_PACKS entry (first install) -- refusing to overwrite another pack/kit's service surface (DGN-1018 §5-B); re-install/upgrade proceeds only when the own id is already recorded"
        fi
      else
      _log "pack: service_facade: $ROOT/service/$_SVC_NS exists and own id '$PACK_ID' is recorded (re-install/upgrade) -- proceeding"
      fi
    fi
    _log "$PACK_CLASS: step service_facade: service/$_SVC_NS/ copy"
    mkdir -p "$ROOT/service/$_SVC_NS"
    while IFS= read -r _src; do
      [[ -e "$_src" ]] || continue
      [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
      _rel="${_src#"$KIT_PAYLOAD_DIR/service/$_SVC_NS/"}"
      _ct_deliver "$_src" "service/$_SVC_NS/$_rel" || continue
      _log "  service_facade: service/$_SVC_NS/$_rel"
    done < <(find "$KIT_PAYLOAD_DIR/service/$_SVC_NS" -type f)
    _log "$PACK_CLASS: service_facade done"
  else
    _log "$PACK_CLASS: service_facade: no payload/service/${_SVC_NS:-<ns>}/ -- skipping"
  fi

  # ---- kit_mirror: payload/mirror/* -> instance/mirror/ ----
  # Reverse-drift guard (DGN-803/DGN-363): if the instance's sdk_bridge.py
  # pin is AHEAD of the payload's pin, SKIP the entire mirror copy (instance
  # is newer; never downgrade). Extractor mirrors update.sh
  # extract_ver_sdk_bridge_py exactly (DGN-364 2.7b).
  # C1 invariant: copy-if-present only -- NO --delete.
  # Excludes: *.db, *.db-wal, *.db-shm, *.db.bak*, __pycache__, *.pyc
  # DGN-1018: mirror/ is a kit-pack-exclusive source surface (compat-lint C4
  # FAILs it for kind=pack) -- the step is class-gated to kit.
  if [[ "$PACK_CLASS" == "pack" ]]; then
    _log "pack: kit_mirror: kit-exclusive step -- NOT executed (behavior-pack profile, DGN-1018 §5-B)"
  elif [[ -d "$KIT_PAYLOAD_DIR/mirror" ]]; then
    _log "kit: step kit_mirror: mirror/ copy"
    if [[ "$KIT_MIRROR_SKIP" -eq 0 ]]; then
      mkdir -p "$ROOT/mirror"
      while IFS= read -r _src; do
        [[ -e "$_src" ]] || continue
        _bname="$(basename "$_src")"
        [[ "$_bname" == ".gitkeep" ]] && continue
        # Exclude runtime state files and compiled artifacts (C1 invariant).
        [[ "$_bname" == *.db ]] && continue
        [[ "$_bname" == *.db-wal ]] && continue
        [[ "$_bname" == *.db-shm ]] && continue
        [[ "$_bname" == *.db-journal ]] && continue
        [[ "$_bname" == *.db.bak* ]] && continue
        [[ "$_bname" == __pycache__ ]] && continue
        [[ "$_bname" == *.pyc ]] && continue
        [[ "$_bname" == "download.html" ]] && continue
        _rel="${_src#"$KIT_PAYLOAD_DIR/mirror/"}"
        # Also skip files inside __pycache__ directories.
        [[ "$_rel" == */__pycache__/* ]] && continue
        [[ "$_rel" == __pycache__/* ]] && continue
        _ct_deliver "$_src" "mirror/$_rel" || continue
        _log "  kit_mirror: $_rel"
      done < <(find "$KIT_PAYLOAD_DIR/mirror" -type f)
      # B1/B3 (DGN-855): drop sentinel so update.sh 3e-mirror knows this
      # instance's mirror/ is pack-owned.  Written ONLY on a successful delivery
      # (not when the reverse-drift guard SKIPs). Idempotent overwrite is fine.
      # Sentinel is existence-checked by update.sh 3e-mirror (content is a
      # human breadcrumb only); name the owning kit (DGN-1003).
      printf '# %s pack owns mirror/ (DGN-855)\n' "$KIT_NAME" > "$ROOT/mirror/.pack-owned"
      _ledger_record "mirror/.pack-owned"
      _log "kit: kit_mirror done (sentinel $ROOT/mirror/.pack-owned written)"
    else
      _log "kit: kit_mirror SKIPPED (reverse-drift guard: instance sdk_bridge.py pin ahead of payload)"
    fi
  else
    _log "kit: kit_mirror: no payload/mirror/ -- skipping"
  fi

  # ---- skills-bundle: payload/skills-bundle/<N>/* -> instance/.claude/skills-bundle/<N>/ ----
  # DGN-956 sharing_mode: the manifest skills[] block classifies each payload
  # skill as 'own' (per-instance copy -- the pre-DGN-956 loop, byte-identical)
  # or 'share' (crew-shared canonical + instance symlink). Unlisted skill or
  # absent block = own (default byte-invariant, fixture S6). Alphabetical
  # order (sort) for determinism.
  if [[ -d "$KIT_PAYLOAD_DIR/skills-bundle" ]]; then
    _log "$PACK_CLASS: step skills-bundle: .claude/skills-bundle/ install"
    mkdir -p "$ROOT/.claude/skills-bundle"
    _share_n=0
    while IFS= read -r _skill_dir; do
      [[ -d "$_skill_dir" ]] || continue
      _skill_name="$(basename "$_skill_dir")"
      _dst_dir="$ROOT/.claude/skills-bundle/$_skill_name"
      if [[ "$(_kit_skill_mode "$_skill_name")" == "share" ]]; then
        # DGN-1018: the crew-shared skill root is keyed by kit identity and a
        # behavior pack has none -- share for class=pack is undefined scope,
        # refused fail-closed (DGN-1004 direction; classify the skill as
        # sharing_mode=own instead).
        if [[ "$PACK_CLASS" == "pack" ]]; then
          _fail "pack: skills-bundle: $_skill_name declares sharing_mode=share -- undefined for a behavior pack (no kit-keyed shared root); use sharing_mode=own (DGN-1018 §5-B)"
        fi
        _kit_share_skill "$_skill_dir" "$_skill_name"
        _ledger_record ".claude/skills-bundle/$_skill_name"
        _share_n=$((_share_n + 1))
      else
        # own guard (R1): cp -f writes THROUGH a symlink, so an 'own' skill
        # whose instance path is a link (e.g. previously classified 'share')
        # would silently overwrite the crew-shared canonical with this
        # instance's bytes. Fail loud; reclassification needs manual cleanup.
        if [[ -L "$_dst_dir" ]]; then
          _fail "kit: skills-bundle: $_skill_name is 'own' but $_dst_dir is a symlink -- refusing write-through; remove the link first (was this skill 'share' before?)"
        fi
        mkdir -p "$_dst_dir"
        while IFS= read -r _src; do
          [[ -e "$_src" ]] || continue
          [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
          _rel="${_src#"$_skill_dir/"}"
          _ct_deliver "$_src" ".claude/skills-bundle/$_skill_name/$_rel" || continue
          _log "  skills-bundle: $_skill_name/$_rel"
        done < <(find "$_skill_dir" -type f)
      fi

      # ---- activation symlink: .claude/skills/<id> -> ../skills-bundle/<id> ----
      # DGN-1409: the legacy agent/module path (STEP 7) has always made the
      # installed skill DISCOVERABLE by linking .claude/skills/<id> to the
      # skills-bundle body -- skills-bundle/ alone is inert storage, this link
      # is what Claude Code actually reads. The contract-class path copied
      # skills-bundle/ content (own and share alike) but never created this
      # link, so a contract pack's skills installed but were never activated.
      # Byte-identical port of the legacy symlink block (own AND share modes
      # land the same instance-relative target -- a share-mode skills-bundle
      # entry may itself be a symlink into $SHARED_ROOT, and linking through
      # it is still a valid two-hop symlink chain).
      # DGN-1840: only the ACTIVE set is linked. A delivered-only skill keeps
      # whatever link state it has: switching off is dogany-lifekit-setup's
      # act (owner state), never a side effect of an install.
      if ! _skill_is_active "$_skill_name"; then
        _log "  $PACK_CLASS: skills-bundle: $_skill_name delivered, not activated (kit conf selection)"
        continue
      fi
      mkdir -p "$ROOT/.claude/skills"
      _link_path="$ROOT/.claude/skills/$_skill_name"
      _link_target="../skills-bundle/$_skill_name"
      if [[ -L "$_link_path" ]]; then
        if [[ "$(readlink "$_link_path")" != "$_link_target" ]]; then
          ln -sfn "$_link_target" "$_link_path"
          _log "  $PACK_CLASS: skills-bundle: re-linked .claude/skills/$_skill_name -> $_link_target"
        fi
      else
        # DGN-1798: fail-closed -- a real dir/file here is owner-made; never remove it.
        [[ -e "$_link_path" ]] && _fail "$PACK_CLASS: skills-bundle: $_link_path is a REAL directory, not a symlink -- refusing to touch it (owner customization). Move or rename it (or delete it yourself if unwanted), then re-run the install"
        ln -sfn "$_link_target" "$_link_path"
        _log "  $PACK_CLASS: skills-bundle: linked .claude/skills/$_skill_name -> $_link_target"
      fi
      _ledger_record ".claude/skills/$_skill_name"
    done < <(_selected_skill_dirs)
    if [[ "$_share_n" -gt 0 ]]; then
      _log "kit: skills-bundle: share summary: $_share_n skill(s) referenced from $SHARED_ROOT"
    fi
    _log "$PACK_CLASS: skills-bundle done"
  else
    _log "$PACK_CLASS: skills-bundle: no payload/skills-bundle/ -- skipping"
  fi

  # ---- DGN-1422: declared-vs-real skill retire ------------------------------
  # The DGN-1409 counterpart: that change made a NEWLY declared skill
  # activate; nothing removed a skill DROPPED from a newer manifest. This
  # pack's own record of what it declared last time
  # (config/packs/<id>.skills) is compared against what this payload
  # declares NOW -- names present in the old record but absent from the new
  # one are retired (archived, never bare-deleted -- lib/skill_retire.sh).
  # Use the pre-delivery selection, never reconstruct it from live links.
  _skills_declared_file="$ROOT/config/packs/$PACK_ID.skills"
  _retired_names="$(skill_retire_diff "$_skills_declared_file" "$_new_skill_names")"
  if [[ -n "$_retired_names" ]]; then
    _retire_ts="$(date +%Y%m%d-%H%M%S)"
    while IFS= read -r _rn; do
      [[ -n "$_rn" ]] || continue
      # DGN-1678: another owner (kit / sibling pack / framework bundle) that
      # declares this name keeps it live -- dropping it from THIS pack's
      # declaration must not archive their asset.
      if _rn_owner="$(skill_retire_other_owner "$ROOT" "$_rn" "$PACK_ID")"; then
        _log "  skill-retire: $_rn: SKIP archive -- still declared by $_rn_owner"
        continue
      fi
      # DGN-1526 R2: an owner-held (local layer) skill is the owner's now,
      # not this pack's -- dropping it from the declaration must not move it.
      if _local_layer_touches ".claude/skills-bundle/$_rn" || _local_layer_touches ".claude/skills/$_rn"; then
        _log "  skill-retire: $_rn: SKIP archive -- held by the owner's local layer (.claude/.dogany-preserve)"
        continue
      fi
      while IFS= read -r _rl; do _log "  skill-retire: $_rl"; done < <(skill_retire_apply "$ROOT" "$(_retire_shared_root "$_rn")" "$_rn" "$_retire_ts")
    done <<< "$_retired_names"
  else
    _log "$PACK_CLASS: skill-retire: no skills dropped from this pack's declaration -- 0 removed"
  fi
  skill_retire_write_declared "$_skills_declared_file" "$_new_skill_names"

  # ---- routines/bundle: payload/routines/bundle/* -> instance/routines/bundle/ ----
  if [[ -d "$KIT_PAYLOAD_DIR/routines/bundle" ]]; then
    _log "$PACK_CLASS: step routines-bundle: routines/bundle/ install"
    mkdir -p "$ROOT/routines/bundle"
    while IFS= read -r _src; do
      [[ -e "$_src" ]] || continue
      [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
      _rel="${_src#"$KIT_PAYLOAD_DIR/routines/bundle/"}"
      _dst="$ROOT/routines/bundle/$_rel"
      _ct_deliver "$_src" "routines/bundle/$_rel" || continue
      [[ "$_dst" == *.sh ]] && chmod +x "$_dst"
      _log "  routines-bundle: $_rel"
    done < <(find "$KIT_PAYLOAD_DIR/routines/bundle" -type f)
    _log "$PACK_CLASS: routines-bundle done"
  else
    _log "$PACK_CLASS: routines-bundle: no payload/routines/bundle/ -- skipping"
  fi

  # ---- hook wiring: manifest hooks[] -> .claude/settings.local.json --------
  # Declaration-driven (DGN-1496): the pack declares {event, script}; the
  # installer wires exactly that, idempotently, right after the
  # routines-bundle files land. Contract surface = routines/bundle/*
  # (compat-lint C4 lockstep). The wire TARGET is the instance-owned
  # settings.local.json, never the framework-owned settings.json that
  # update.sh regenerates wholesale (DGN-1250 / DGN-1665, see _hooks_wire).
  # This inline validation is deliberately INDEPENDENT of the compat-lint
  # C-HOOKS gate: the install-side lint gate is structurally fail-open when
  # compat-lint.sh is absent (B4 precedent), and install-side is the only
  # gate a third-party pack passes through.
  if _hooks_declared "$MANIFEST"; then
    _log "$PACK_CLASS: step hook-wiring: declared hook wiring (manifest hooks[])"
    _hooks_validate "$PACK_CLASS: hook-wiring" "$MANIFEST" "$KIT_PAYLOAD_DIR" bundle
    _hooks_wire "$PACK_CLASS: hook-wiring" "$ROOT/.claude/settings.local.json" "$ROOT" "$_HOOK_LINES"
    _ledger_note merged ".claude/settings.local.json"
    _census_record hook_wiring "$_HOOKS_WIRE_OUTCOME"
    _log "$PACK_CLASS: hook-wiring done"
  else
    _census_record hook_wiring "skip:no-manifest-hooks"
  fi

  # ---- routines-schedules: payload/routines/*.plist -> instance/routines/ ----
  # DGN-1402: kit-declared launchd schedules (capabilities.schedules=true,
  # kit-exclusive -- KIT_SCHEDULES is never 1 for a behavior pack). Delivery
  # only: filename + content rendering + plists.defer merge-append, the same
  # three things legacy STEP 2c already does for the agent/module path. No
  # launchctl call happens here -- actual load stays the later, explicit,
  # conversational step this framework already uses for every other
  # schedule (routine-ctl.sh / brief-slot-ctl.sh, unchanged machinery); a
  # plist landing here is inert until that step runs.
  if [[ "$KIT_SCHEDULES" -eq 1 && -d "$KIT_PAYLOAD_DIR/routines" ]]; then
    _log "kit: step routines-schedules: routines/*.plist install"
    mkdir -p "$ROOT/routines"
    while IFS= read -r _sd_src; do
      [[ -e "$_sd_src" ]] || continue
      _sd_dst_base="$(_subst_mint_agent_name_basename "$(basename "$_sd_src")")"
      _sd_dst="$ROOT/routines/$_sd_dst_base"
      _ct_deliver "$_sd_src" "routines/$_sd_dst_base" || continue
      # Content gets both substitutions: the literal 'telegram-agent' ->
      # real agent name (same convention as the basename and plists.defer --
      # compat-lint C5 gate(b) forbids a kit payload from carrying the
      # __AGENT_NAME__ persona token, so this is the only agent-name
      # placeholder a plist body may use) and __PROJECT_ROOT__/__HOME__ via
      # the shared mint-token substitution (not persona tokens, C5-clean).
      _subst_mint_agent_name_content "$_sd_dst"
      _subst_mint_tokens "$_sd_dst"
      _log "  routines-schedules: routines/$_sd_dst_base (rendered)"
    done < <(find "$KIT_PAYLOAD_DIR/routines" -maxdepth 1 -type f -name '*.plist' | sort)
    # plists.defer merge-append (DGN-227 MINOR-5 policy, ported verbatim from
    # legacy STEP 2c): a pack-shipped defer fragment never CLOBBERS the
    # instance's framework defer manifest (which already stages the
    # generic-brief units, DGN-1286 item 2b) -- entries are appended,
    # duplicates skipped.
    if [[ -f "$KIT_PAYLOAD_DIR/routines/plists.defer" ]]; then
      _sd_pack_defer="$(mktemp)"
      cp -f "$KIT_PAYLOAD_DIR/routines/plists.defer" "$_sd_pack_defer"
      _subst_mint_agent_name_content "$_sd_pack_defer"
      _sd_inst_defer="$ROOT/routines/plists.defer"
      _ledger_note merged "routines/plists.defer"
      if [[ ! -f "$_sd_inst_defer" ]]; then
        atomic_cp "$_sd_pack_defer" "$_sd_inst_defer" || _fail "$PACK_CLASS: copy failed for routines/plists.defer -- install aborted"
        _log "  routines-schedules: installed routines/plists.defer (no prior instance defer -- pack defer adopted)"
      else
        _sd_appended=0
        _sd_marker="# --- pack:$PACK_ID defer entries (DGN-227 MINOR-5 merge-append) ---"
        while IFS= read -r _sd_de || [[ -n "$_sd_de" ]]; do
          case "$_sd_de" in ''|'#'*) continue ;; esac
          if grep -qxF "$_sd_de" "$_sd_inst_defer"; then
            _log "  routines-schedules: defer merge: entry already present, skipping: $_sd_de"
            continue
          fi
          if [[ "$_sd_appended" -eq 0 ]]; then
            grep -qxF "$_sd_marker" "$_sd_inst_defer" || printf '%s\n' "$_sd_marker" >> "$_sd_inst_defer"
            _sd_appended=1
          fi
          printf '%s\n' "$_sd_de" >> "$_sd_inst_defer"
          _log "  routines-schedules: defer merge: appended pack entry: $_sd_de"
        done < "$_sd_pack_defer"
        [[ "$_sd_appended" -eq 1 ]] || _log "  routines-schedules: defer merge: all pack entries already present (idempotent, no change)"
      fi
      rm -f "$_sd_pack_defer"
    fi
    _log "kit: routines-schedules done"
  else
    [[ "$KIT_SCHEDULES" -eq 1 ]] && _log "kit: routines-schedules: no payload/routines/*.plist -- skipping (dead declaration already caught above)"
  fi

  # ---- config merge-key: payload/config/{<kit>.conf,i18n*} ----
  # Merge-key: only adds keys absent from the instance config (never clobbers).
  # DGN-1003: the kit conf filename derives from KIT_NAME (compat-lint C4
  # allows exactly config/<kit>.conf plus i18n fragments -- lockstep).
  # Config owner name (DGN-1018 §4, lockstep with compat-lint C4 CONF_OWNER):
  # kit => <KIT_NAME>.conf, pack => <manifest id>.conf (existing id field
  # reused, no new field).
  _CONF_OWNER="$KIT_NAME"
  if [[ "$PACK_CLASS" == "pack" ]]; then
    _CONF_OWNER="$(_mf_field id)"
  fi
  if [[ -d "$KIT_PAYLOAD_DIR/config" ]]; then
    _log "$PACK_CLASS: step config-merge: config/ merge-key"
    # <owner>.conf -> config/<owner>.conf
    if [[ -n "$_CONF_OWNER" && -f "$KIT_PAYLOAD_DIR/config/$_CONF_OWNER.conf" ]]; then
      _merge_out="$(_kit_conf_merge "$ROOT/config/$_CONF_OWNER.conf" "$KIT_PAYLOAD_DIR/config/$_CONF_OWNER.conf")"
      while IFS= read -r _ml; do [[ -n "$_ml" ]] && _log "  config-merge $_CONF_OWNER.conf: $_ml"; done <<< "$_merge_out"
      _ledger_note merged "config/$_CONF_OWNER.conf"
    fi
    # i18n fragment files at config/ top level (KEY=VALUE format)
    while IFS= read -r _src; do
      [[ -e "$_src" ]] || continue
      [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
      _fname="$(basename "$_src")"
      _merge_out="$(_kit_conf_merge "$ROOT/config/$_fname" "$_src")"
      while IFS= read -r _ml; do [[ -n "$_ml" ]] && _log "  config-merge $_fname: $_ml"; done <<< "$_merge_out"
      _ledger_note merged "config/$_fname"
    done < <(find "$KIT_PAYLOAD_DIR/config" -maxdepth 1 -type f \( -name 'i18n.*' -o -name 'i18n_*' \))
    _log "$PACK_CLASS: config-merge done"
  else
    _log "$PACK_CLASS: config-merge: no payload/config/ -- skipping"
  fi

  # ---- i18n override layer: payload/config/i18n/<lang>.bundle.json ----
  # DGN-1789 (owner lock dec-204): the framework ships i18n keys + defaults
  # in config/i18n/<lang>.json; a kit overrides only the keys it needs, in
  # its OWN layer config/packs/<pack-id>.i18n.<lang>.json; the shared lookup
  # returns the kit override if present, else the framework default.
  # WHY NOT merge into config/i18n/<lang>.json (the old DGN-803 add-only
  # merge, removed here): that file is framework-owned and every framework
  # update rewrites it, so merged kit keys vanished on the next update
  # (measured: main agent kit_life remind.prep_start gone -> the English literal
  # "Reminder" on a ko instance). Do not reintroduce a write into
  # config/i18n/ from a pack, and do not "replay" a merge after update.
  # Replace semantics per pack: this pack's layers are rewritten from the
  # payload, and a language the payload no longer ships loses its layer.
  # DGN-1789 r3: the manifest goes along so the writer records the layer
  # order (config/packs/$PACK_ID.i18n_order: provides_kit / requires_kit.kit)
  # -- a pack extending a kit must read above that kit, and the lookup has
  # no manifest on the instance.
  if [[ -d "$KIT_PAYLOAD_DIR/config/i18n" ]] \
     || compgen -G "$ROOT/config/packs/$PACK_ID.i18n.*.json" >/dev/null; then
    _log "$PACK_CLASS: step i18n-layer: config/i18n/*.bundle.json -> config/packs/$PACK_ID.i18n.<lang>.json"
    [[ -f "$KIT_I18N_LAYER_LIB" ]] || _fail "i18n-layer: writer lib missing ($KIT_I18N_LAYER_LIB)"
    _layer_rc=0
    _layer_out="$("$PACK_PY3" "$KIT_I18N_LAYER_LIB" sync "$ROOT/config/packs" "$PACK_ID" \
                    "$KIT_PAYLOAD_DIR/config/i18n" "$MANIFEST")" || _layer_rc=$?
    while IFS= read -r _l; do [[ -n "$_l" ]] && _log "  $_l"; done <<< "$_layer_out"
    [[ "$_layer_rc" -eq 0 ]] || _fail "i18n-layer: writer refused (rc=$_layer_rc) -- see the [kit-i18n-layer] line above"
    for _layer_f in "$ROOT/config/packs/$PACK_ID".i18n.*.json; do
      [[ -f "$_layer_f" ]] && _ledger_record "config/packs/$(basename "$_layer_f")"
    done
    _log "$PACK_CLASS: i18n-layer done"
  fi

  # ---- term dictionary layer: payload/config/terms.json ----
  # DGN-1500: one dictionary file per layer, shipped by that layer's owner.
  # The framework layer rides the template (config/terms/framework.json);
  # a kit/pack ships payload/config/terms.json and the writer installs it
  # as config/packs/$PACK_ID.terms.json stamped layer=kit (kind=kit) or
  # layer=module (kind=pack), owner=$PACK_ID. The send-time loader
  # (routines/lib/term_dict.py) reads only the layers present on this
  # instance -- that is what scopes an entry to agents that have the layer.
  # Replace semantics: a payload that stops shipping the file loses its
  # layer. Same validator as the loader: a bad file refuses the install.
  if [[ -f "$KIT_PAYLOAD_DIR/config/terms.json" ]] \
     || [[ -f "$ROOT/config/packs/$PACK_ID.terms.json" ]]; then
    _log "$PACK_CLASS: step term-dict-layer: config/terms.json -> config/packs/$PACK_ID.terms.json"
    [[ -f "$TERM_DICT_LAYER_LIB" ]] || _fail "term-dict-layer: writer lib missing ($TERM_DICT_LAYER_LIB)"
    _td_rc=0
    _td_out="$("$PACK_PY3" "$TERM_DICT_LAYER_LIB" sync "$ROOT/config/packs" "$PACK_ID" \
                 "$KIT_PAYLOAD_DIR/config/terms.json" "$MANIFEST")" || _td_rc=$?
    while IFS= read -r _l; do [[ -n "$_l" ]] && _log "  $_l"; done <<< "$_td_out"
    [[ "$_td_rc" -eq 0 ]] || _fail "term-dict-layer: writer refused (rc=$_td_rc) -- see the [term-dict-layer] line above"
    _log "$PACK_CLASS: term-dict-layer done"
  fi

  # ---- D7 reviewed-outbound fragment: payload/config/outbound-allowlist ----
  # DGN-1860: a kit/pack vouches for the reviewed outbound primitives in its
  # OWN payload files. Installed as config/packs/$PACK_ID.outbound-allowlist,
  # which the secret-sweep outbound-diff check merges additively (exact
  # path match; entries outside the kit delivery surfaces are ignored there).
  # Because the fragment ships with the payload, a skill rename moves its
  # approval with it -- the path-keyed owner .outbound-allowlist could not.
  # Replace semantics: a payload that stops shipping the file loses its
  # layer. The owner's .outbound-allowlist is never read or written here.
  if [[ -f "$KIT_PAYLOAD_DIR/config/outbound-allowlist" ]]; then
    _log "$PACK_CLASS: step outbound-allowlist: config/outbound-allowlist -> config/packs/$PACK_ID.outbound-allowlist"
    _ct_deliver "$KIT_PAYLOAD_DIR/config/outbound-allowlist" "config/packs/$PACK_ID.outbound-allowlist" || true
  elif [[ -f "$ROOT/config/packs/$PACK_ID.outbound-allowlist" ]]; then
    rm -f "$ROOT/config/packs/$PACK_ID.outbound-allowlist"
    _log "$PACK_CLASS: step outbound-allowlist: payload ships none -> removed config/packs/$PACK_ID.outbound-allowlist"
  fi

  # ---- deps-provision: payload/requirements.txt -> runtime interpreters ----
  # DGN-850 kit<->pack dependency seam. Deterministic + idempotent + logged;
  # a provisioning failure NEVER fails the kit install (the provisioner exits
  # 0 on degrade by contract; '|| true' is belt-and-suspenders under set -e).
  # No payload/requirements.txt = the normal zero-delta state of a pack with
  # no python deps -> explicit no-op log line inside the provisioner.
  _DEPS_SH="$SCRIPT_DIR/pack_deps_provision.sh"
  if [[ -f "$_DEPS_SH" ]]; then
    _log "$PACK_CLASS: step deps-provision (DGN-850)"
    _deps_out="$(bash "$_DEPS_SH" --root "$ROOT" --requirements "$KIT_PAYLOAD_DIR/requirements.txt" 2>&1)" || true
    while IFS= read -r _dl; do _log "  $_dl"; done <<< "$_deps_out"
  else
    _log "$PACK_CLASS: deps-provision WARN -- pack_deps_provision.sh not found at $_DEPS_SH (skipped; consumers fall back)"
  fi

  # ---- knowledge warehouse: payload/knowledge/<name>/* -> instance/knowledge/<name>/ ----
  # DGN-1426: Layer-1 delivery for the contract-class path (was a SHAPE-ONLY
  # stub -- DGN-1018 §7 residual 1 / DGN-807 D1-D6 deferral -- see the
  # DGN-1426 design record). Declaration precedes surface
  # (§4/§5-1 doctrine, enforced above): KNOW_WH is empty unless the manifest
  # names a warehouse AND payload/knowledge/<name>/ exists on disk -- a dead
  # declaration or an undeclared knowledge/ payload already BLOCKED before
  # this point, so reaching here with KNOW_WH set means the surface is real
  # and consented. Plain additive cp -f loop -- same convention as the
  # skills-bundle/routines-bundle steps above (no rsync/--delete mirror, no
  # destination-ahead regression guard: unlike the legacy knowledge-snapshot.sh
  # channel there is no instance-side accretion to protect yet, since Layer 3
  # consumer wiring for the contract-class path is out of this ticket's scope
  # -- see the design note for what a future re-delivery guard would need).
  if [[ "$PACK_CLASS" == "pack" && -n "$KNOW_WH" ]]; then
    _log "pack: step knowledge-warehouse: knowledge/$KNOW_WH/ install"
    mkdir -p "$ROOT/knowledge/$KNOW_WH"
    while IFS= read -r _src; do
      [[ -e "$_src" ]] || continue
      [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
      _rel="${_src#"$KIT_PAYLOAD_DIR/knowledge/$KNOW_WH/"}"
      _ct_deliver "$_src" "knowledge/$KNOW_WH/$_rel" || continue
      _log "  knowledge-warehouse: $_rel"
    done < <(find "$KIT_PAYLOAD_DIR/knowledge/$KNOW_WH" -type f)
    {
      echo "warehouse: $KNOW_WH"
      echo "release: $PACK_VERSION"
      echo "snapshot_date: $(date -u +%F)"
      echo "source: pack:$PACK_ID@$PACK_VERSION"
    } > "$ROOT/knowledge/$KNOW_WH/.snapshot-pin"
    _ledger_record "knowledge/$KNOW_WH/.snapshot-pin"
    _log "pack: knowledge-warehouse done ($ROOT/knowledge/$KNOW_WH/.snapshot-pin written)"
  elif [[ "$PACK_CLASS" == "pack" ]]; then
    _log "pack: knowledge-warehouse: no knowledge_warehouse declared -- knowledge/ NOT installed (absence signal, DGN-1426)"
  fi

  # ---- units engrave: manifest units block -> .instance.conf (DGN-783 B4) ----
  # Additive vocabulary seam: units:{primary,set} is an OPTIONAL manifest
  # block (contract_version stays 1; C1 reads only its 4 fields and ignores
  # it). When present, engrave KIT_UNIT_PRIMARY / KIT_UNIT_SET /
  # KIT_UNITS_KIT with replace-upsert semantics: a re-install must never
  # leave a stale value behind (add-only merge would freeze the first value
  # forever). Absent -> no engrave; display falls back to the i18n key
  # unit.generic.
  # DGN-1018: units is kit vocabulary (KIT_UNITS_KIT names the owning kit) --
  # the engrave step is class-gated to kit.
  if [[ "$PACK_CLASS" == "kit" ]]; then
  "$PACK_PY3" - "$ROOT/.instance.conf" "$MANIFEST" "$KIT_NAME" <<'PYEOF'
import json, sys, pathlib
conf = pathlib.Path(sys.argv[1])
manifest = json.load(open(sys.argv[2]))
kit = sys.argv[3]
units = manifest.get("units")
if not isinstance(units, dict):
    print("[units-engrave] no units block in manifest -- skipped (fallback: unit.generic)")
    sys.exit(0)
primary = units.get("primary") or ""
uset = units.get("set") or []
if isinstance(uset, str):
    uset = [uset]
pairs = {
    "KIT_UNIT_PRIMARY": str(primary),
    "KIT_UNIT_SET": ",".join(str(u) for u in uset),
    "KIT_UNITS_KIT": kit,
}
lines = conf.read_text(encoding="utf-8").splitlines() if conf.exists() else []
seen = set()
for i, ln in enumerate(lines):
    key = ln.split("=", 1)[0]
    if key in pairs and key not in seen:
        lines[i] = f"{key}={pairs[key]}"  # replace-upsert: clobber stale value
        seen.add(key)
for key in ("KIT_UNIT_PRIMARY", "KIT_UNIT_SET", "KIT_UNITS_KIT"):
    if key not in seen:
        lines.append(f"{key}={pairs[key]}")
conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("[units-engrave] engraved: " + ", ".join(f"{k}={v}" for k, v in pairs.items()))
PYEOF
  _log "  units engrave step done (units block optional; replace-upsert)"
  else
    _log "pack: units engrave: kit vocabulary seam -- not applicable (class=pack)"
  fi

  # ---- DGN-1467: delivery tail (was stranded below the legacy exit 0) -------
  # Contract-class installs used to end at the DONE block below without ever
  # reaching the legacy region's delivery steps -- step 3 (agent.conf
  # fragment), step 7b (persona fragment), step 7c (knowledge wiring
  # selftest) sat 1000+ lines past the contract `exit 0`. The steps now live
  # in the shared functions above; this path calls them with contract inputs:
  #   * payload/config/agent.conf.add -- instance-conf keys whose consumers
  #     read config/agent.conf (the config-merge step lands at
  #     config/<owner>.conf, which knowledge_selftest.sh G3 and the
  #     dogany-memory-search SKILL.md never read -- the dgn1426 §6 bypass
  #     was a live error, not a stale note);
  #   * payload/AGENT.md.add -- pack persona prose + knowledge pointer,
  #     mint-token rendered (_subst_mint_tokens);
  #   * knowledge_selftest.sh -- contract-aware lane (DGN-1467).
  # Both fragment surfaces are presence-driven at the payload root/config
  # (requirements.txt precedent, DGN-850) and allowlisted in compat-lint C4.

  # agent.conf fragment (contract surface: payload/config/agent.conf.add)
  _CT_CONF_ADD="$KIT_PAYLOAD_DIR/config/agent.conf.add"
  if [[ -f "$_CT_CONF_ADD" ]]; then
    _log "$PACK_CLASS: step agent-conf-fragment: config/agent.conf append"
    mkdir -p "$ROOT/config"
    _CT_CONF_BODY="$(mktemp)"
    # comment-only/blank lines stripped + migration-family keys refused,
    # same as the legacy fresh path (peer keys are legacy migration
    # vocabulary; a contract fragment must not smuggle them).
    grep -v '^#' "$_CT_CONF_ADD" | grep -v '^$' | grep -Ev "$PEER_KEYS_RE" > "$_CT_CONF_BODY" || true
    _subst_mint_tokens "$_CT_CONF_BODY"
    _mint_residue_check "$PACK_CLASS: agent-conf-fragment" "$_CT_CONF_BODY"
    _conf_fragment_apply "$PACK_CLASS: agent-conf-fragment" "$ROOT/config/agent.conf" "$_CT_CONF_BODY" \
      "$PACK_ID" "" contract \
      "  appended config/agent.conf.add (contract surface, marker pair)"
    _ledger_note merged "config/agent.conf"
    rm -f "$_CT_CONF_BODY"
    if [[ "$_FRAG_OUTCOME" == "already-present" ]]; then
      _frag_version_guard "$PACK_CLASS: agent-conf-fragment"
      _census_record agent_conf_fragment already-present
    else
      _census_record agent_conf_fragment run
    fi
    _log "$PACK_CLASS: agent-conf-fragment done"
  else
    _census_record agent_conf_fragment "skip:no-payload-config/agent.conf.add"
  fi

  # persona fragment (contract surface: payload/AGENT.md.add)
  _CT_AGENT_ADD="$KIT_PAYLOAD_DIR/AGENT.md.add"
  if [[ -f "$_CT_AGENT_ADD" ]]; then
    _log "$PACK_CLASS: step persona-fragment: persona fragment append"
    # DGN-773 R5/T4b: resolve to the REAL persona file (identity/
    # hot.custom.agent.md > PROFILE.md > AGENT.md) -- never write through
    # the AGENT.md compat symlink path.
    _CT_PERSONA_MD="$(resolve_persona_md "$ROOT")"
    if [[ ! -f "$_CT_PERSONA_MD" ]]; then
      _fail "$PACK_CLASS: persona-fragment: persona file not found at $_CT_PERSONA_MD (mint incomplete?)"
    fi
    _CT_RENDERED="$(mktemp)"
    cp "$_CT_AGENT_ADD" "$_CT_RENDERED"
    _subst_mint_tokens "$_CT_RENDERED"
    _mint_residue_check "$PACK_CLASS: persona-fragment" "$_CT_RENDERED"
    _persona_fragment_apply "$PACK_CLASS: persona-fragment" "$_CT_PERSONA_MD" "$_CT_RENDERED" \
      "$PACK_ID" "" contract \
      "  appended AGENT.md.add (mint-token rendered, marker pair) to $_CT_PERSONA_MD"
    _ledger_note merged "${_CT_PERSONA_MD#"$ROOT"/}"
    rm -f "$_CT_RENDERED"
    if [[ "$_FRAG_OUTCOME" == "already-present" ]]; then
      _frag_version_guard "$PACK_CLASS: persona-fragment"
      _census_record persona_fragment already-present
    else
      _census_record persona_fragment run
    fi
    _log "$PACK_CLASS: persona-fragment done"
  else
    _census_record persona_fragment "skip:no-payload-AGENT.md.add"
  fi

  # knowledge wiring selftest (contract lane). The expectation flags tell
  # the selftest which wiring artifacts THIS install just delivered, so
  # their absence is a delivery-machinery FAIL, not a "pack ships no such
  # surface" skip-note.
  _CT_SELFTEST_ARGS=""
  if [[ -f "$_CT_CONF_ADD" ]] && grep -Eq '^KNOWLEDGE_WAREHOUSE=' "$_CT_CONF_ADD"; then
    _CT_SELFTEST_ARGS="$_CT_SELFTEST_ARGS --expect-conf-key"
  fi
  if [[ -f "$_CT_AGENT_ADD" ]] && grep -qF 'KNOWLEDGE-WIRING-POINTER' "$_CT_AGENT_ADD"; then
    _CT_SELFTEST_ARGS="$_CT_SELFTEST_ARGS --expect-persona-pointer"
  fi
  _log "$PACK_CLASS: step knowledge-selftest: wiring gates (DGN-402/DGN-1467)"
  # shellcheck disable=SC2086 -- flag list is word-split on purpose
  _knowledge_selftest_run "$PACK_CLASS: knowledge-selftest" $_CT_SELFTEST_ARGS
  _census_record knowledge_selftest run
  _log "$PACK_CLASS: knowledge-selftest done"

  # steps that exist in NO install path yet (DGN-1467 design v1 §1 D-class):
  # rostered as deferred so the non-delivery is RECORDED on every install
  # instead of staying silent. Ticket closure promotes the entry to a real
  # step + verdict=run.
  _census_record crew_assets "deferred:DGN-1060"
  _census_record mailbox_provision "deferred:DGN-1125"
  # DGN-1467 grill roster additions: two legacy steps whose "no migration
  # needed" verdict was OVERTURNED on re-audit. Bridge start is deliberately
  # caller-owned: the pipeline starts after it closes its transaction, while
  # a direct installer caller must explicitly run `mint_run.sh start`. State
  # accept exists only on the legacy path;
  # the contract path leans on the mint-agent SKILL.md checklist (= model
  # judgment, against the "detection lives outside model judgment"
  # doctrine). Rostered deferred so the non-delivery is RECORDED on every
  # install until the repair tickets land (evidence: the DGN-1467 grill report
  # dated 2026-09-15 -- cited by ticket, never by path: the DGN-1123 gate
  # forbids arm-state-tree references in this installer, comments included,
  # and it is right to be literal about it).
  _census_record bot_start "caller-owned:mint_run.sh-start"
  _census_record minting_state_accept "deferred:DGN-1467-grill#step11"

  # ---- delivery census seal (fail-closed, DGN-1467) ----
  for _ct_step in $_DELIVERY_ROSTER; do
    if ! printf '%s' "$_DELIVERY_CENSUS" | grep -q "^${_ct_step}"$'\t'; then
      _fail "delivery census: roster step '$_ct_step' left NO verdict -- a delivery step was not reached; refusing to complete the install (fail-closed, DGN-1467)"
    fi
  done
  _CT_TSV_TMP="$(mktemp)"
  {
    printf '# delivery census -- pack=%s@%s class=%s (DGN-1467)\n' "$PACK_ID" "$RECORD_VERSION" "$PACK_CLASS"
    printf '%s' "$_DELIVERY_CENSUS"
  } > "$_CT_TSV_TMP"
  mkdir -p "$ROOT/config/packs"
  mv "$_CT_TSV_TMP" "$ROOT/config/packs/$PACK_ID.delivery.tsv"
  _log "  delivery census sealed: config/packs/$PACK_ID.delivery.tsv ($(printf '%s' "$_DELIVERY_CENSUS" | grep -c . | tr -d ' ') verdict(s))"

  # ---- DGN-1526 R1: installed-files ledger (record only) ----
  _ledger_finalize

  # ---- DGN-1456/1460: promote the evidence BEFORE the ledger projection ----
  # Every delivery step above has run; re-verify the expected-anchor list
  # fixed before the first byte landed, then issue delivery-complete. Order
  # matters (report §4.4): receipt first, DOGANY_PACKS second -- a crash in
  # between leaves ledger(old) != receipt(new), which the reader consumes as
  # MISMATCH (fail-closed), never as a completed install. A refused
  # completion keeps the in-progress marker AND the old ledger line: the
  # ledger never claims what the evidence cannot back.
  _ev_rc=0
  _ev_out="$(evidence_complete "$ROOT" "$PACK_ID")" || _ev_rc=$?
  while IFS= read -r _ev_l; do [[ -n "$_ev_l" ]] && _log "  $_ev_l"; done <<< "$_ev_out"
  if [[ "$_ev_rc" -ne 0 ]]; then
    _fail "install evidence: delivery-complete REFUSED (expected payload missing after delivery) -- ledger NOT updated, in-progress marker kept (DGN-1460 fail-closed)"
  fi

  # ---- DOGANY_PACKS upsert (existing machinery, DGN-227 B3/P7) ----
  # TK-13 U1: atomic primitive (flock + tmp/fsync/rename) -- same-wire with
  # the agent/module path upsert below (lock L216 mandatory co-repair).
  # RECORD_VERSION, not PACK_VERSION: the ledger records the version of the
  # tag the payload came from (--source-tag, dsp t2pin); identical to
  # PACK_VERSION when no tag was named.
  _packs_upsert_atomic "$ROOT/.instance.conf" "$PACK_ID" "$RECORD_VERSION"
  _log "  .instance.conf DOGANY_PACKS upserted ($PACK_ID@$RECORD_VERSION)"

  # A successful shared-DB install must be visible to all three planner
  # sources. No estate scan: add this member only, preserving every peer.
  if [[ "$PACK_CLASS" == "kit" && "$KIT_DB_LANE" -eq 1 && -L "$_KIT_DB" ]]; then
    . "$SCRIPT_DIR/lib/line_upsert.sh"
    _line_upsert "$CREW_DIR/$KIT_NAME/crew.conf" "member=$ROOT" "" crew \
      || _fail "kit: crew registration failed"
  fi

  # DGN-1031: requires_framework record for the framework-update-time compat
  # gate (update.sh fw_reqframework_guard). Same-wire with the agent/module
  # path record below.
  _reqfw_record "$ROOT" "$PACK_ID" "$MANIFEST"

  # DGN-1632: post-mint next-step slot record for the DGN-1487 selector
  # (onboarding-check.py resolve_post_mint_next). Same-wire with the agent/
  # module path record below; a write failure must not read as a successful
  # install.
  _post_mint_next_record "$ROOT" "$PACK_ID" "$MANIFEST" \
    || _fail "post_mint_next record write failed for $PACK_ID -- install NOT complete"

  # DGN-1632 W3: pack-owned Q4 tone candidates for newborn onboarding
  # (onboarding-check.py resolve_tone_candidates). Same-wire with the agent/
  # module path record below.
  _tone_candidates_record "$ROOT" "$PACK_ID" "$MANIFEST" \
    || _fail "tone_candidates record write failed for $PACK_ID -- install NOT complete"

  if [[ "$PACK_CLASS" == "kit" ]]; then
    if [[ "$KIT_COMPLETE" -eq 1 ]]; then
      _log "=== kit-install DONE pack=$PACK_ID slug=$SLUG provides_kit=$KIT_NAME complete=1 ==="
      echo "[pack_install] DONE (kit) -- COMPLETE -- see $LOG_FILE"
    else
      _log "=== kit-install DONE pack=$PACK_ID slug=$SLUG provides_kit=$KIT_NAME complete=0 pending_verb='$KIT_COMPLETION_VERB' ==="
      echo "[pack_install] DONE (kit) -- INSTALLED, NOT COMPLETE -- pending data-dependent step: '$KIT_COMPLETION_VERB' -- see $LOG_FILE"
    fi
  else
    _log "=== pack-install DONE (behavior-pack profile) pack=$PACK_ID slug=$SLUG service_namespace='${SERVICE_NS:-}' ==="
    echo "[pack_install] DONE (pack) -- see $LOG_FILE"
  fi
  _log "$PACK_CLASS: bridge start NOT performed; caller owns mint_run.sh start (pipeline runs it after completion)"
  echo "[pack_install] bridge not started; caller must run mint_run.sh start --root $ROOT"
  exit 0
fi

# ---------------------------------------------------------------------------
# END OF KIT/PACK CONTRACT-CLASS PATH -- legacy agent/module installs
# continue below (kind absent + contract_version absent packs, e.g.
# packs/dev -- byte-identical no-regression path, DGN-1018 §5-B).
# ---------------------------------------------------------------------------

PACK_NAME="$(_mf_field name)"
PKG_REF_SLUG="$(_mf_field reference_slug)"
PKG_REF_ROOT="$(_mf_field reference_root)"
PKG_REF_HOME="$(_mf_field reference_home)"
AGENT_MARKER="$(_mf_field agent_md_marker)"
CONF_MARKER="$(_mf_field agent_conf_marker)"
DOMAIN_SEED_DECL="$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if d.get('domain_seed') else 0)" "$MANIFEST")"

# Validate every declared category name against the known whitelist (M3).
# An unknown name is a manifest error -- FATAL at preflight with a clear message.
KNOWN_CATEGORIES="lib routines plists prompts agent_conf_fragment triggers db_migrations skills agent_md_fragment scripts knowledge_snapshot"

_cat_validate_out="$("$PACK_PY3" - "$MANIFEST" "$KNOWN_CATEGORIES" <<'PYEOF'
import json, sys
known = set(sys.argv[2].split())
with open(sys.argv[1]) as f:
    d = json.load(f)
for c in d.get("categories", []):
    name = c["category"]
    if name not in known:
        # Print a sentinel line; exit 0 so set -e does not swallow the message.
        print("UNKNOWN_CATEGORY: manifest category %r is not in the known category whitelist: %s" % (
            name, sys.argv[1]))
        sys.exit(0)
PYEOF
)"
if [[ "$_cat_validate_out" == UNKNOWN_CATEGORY:* ]]; then
  _fail "${_cat_validate_out#UNKNOWN_CATEGORY: }"
fi

# categories as "name<TAB>required(0/1)" lines
CATEGORY_LINES="$("$PACK_PY3" - "$MANIFEST" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
for c in d.get("categories", []):
    print("%s\t%d" % (c["category"], 1 if c.get("required") else 0))
PYEOF
)"

# optional explicit file list for the lib category (backward-compat: the
# health pack lib/ carries peer-side files that must NOT deploy)
LIB_FILES="$("$PACK_PY3" - "$MANIFEST" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
for c in d.get("categories", []):
    if c["category"] == "lib":
        for name in c.get("files", []):
            print(name)
PYEOF
)"

has_cat() { # has_cat <name> -- 0 when the category is declared
  printf '%s\n' "$CATEGORY_LINES" | awk -F'\t' -v c="$1" '$1==c{f=1} END{exit f?0:1}'
}
cat_required() { # cat_required <name> -- 0 when declared required:true
  printf '%s\n' "$CATEGORY_LINES" | awk -F'\t' -v c="$1" '$1==c && $2==1{f=1} END{exit f?0:1}'
}

# ---------- knowledge object (DGN-402 knowledge wiring standard) --------------
# Single source of truth for warehouse wiring = the pack manifest 'knowledge'
# object. It must appear together with the knowledge_snapshot category (half
# declaration = preflight FAIL below). The install path NEVER reads the
# catalog.json knowledge prose (display-only).
KNOWLEDGE_DECL="$("$PACK_PY3" -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if isinstance(d.get('knowledge'), dict) else 0)" "$MANIFEST")"

KNOW_WAREHOUSE="" KNOW_SOURCE="" KNOW_SMOKE_ITEM="" KNOW_SMOKE_ARGS=""
KNOW_CONSUMER_LINES="" KNOW_TURN_LINES="" KNOW_CONSUMER_IDS=""
if [[ "$KNOWLEDGE_DECL" -eq 1 ]]; then
  _know_str() { # _know_str <key> -- knowledge.<key> string field ('' when absent)
    "$PACK_PY3" -c "import json,sys; k=json.load(open(sys.argv[1])).get('knowledge') or {}; v=k.get(sys.argv[2]); print(v if isinstance(v,str) else '')" "$MANIFEST" "$1"
  }
  KNOW_WAREHOUSE="$(_know_str warehouse)"
  KNOW_SOURCE="$(_know_str source)"
  KNOW_SMOKE_ITEM="$(_know_str smoke_item)"
  KNOW_SMOKE_ARGS="$(_know_str smoke_args)"
  # '~' expansion for the publisher source path (spec S1 layer 1)
  KNOW_SOURCE="${KNOW_SOURCE/#\~/$HOME}"
  # consumer_skills as "skill<TAB>domain domain ..." lines
  KNOW_CONSUMER_LINES="$("$PACK_PY3" - "$MANIFEST" <<'PYEOF'
import json, sys
k = json.load(open(sys.argv[1])).get("knowledge") or {}
for skill, domains in (k.get("consumer_skills") or {}).items():
    if not isinstance(domains, list):
        domains = []
    print("%s\t%s" % (skill, " ".join(str(d) for d in domains)))
PYEOF
)"
  # turns as "type<TAB>home" lines
  KNOW_TURN_LINES="$("$PACK_PY3" - "$MANIFEST" <<'PYEOF'
import json, sys
k = json.load(open(sys.argv[1])).get("knowledge") or {}
for t in k.get("turns") or []:
    if isinstance(t, dict):
        print("%s\t%s" % (t.get("type", ""), t.get("home", "")))
PYEOF
)"
  KNOW_CONSUMER_IDS="$(printf '%s\n' "$KNOW_CONSUMER_LINES" | awk -F'\t' 'NF{printf "%s ", $1}')"
fi

_is_consumer_skill() { # _is_consumer_skill <skill-id> -- 0 when in manifest set
  local id="$1" c
  for c in $KNOW_CONSUMER_IDS; do
    [[ "$c" == "$id" ]] && return 0
  done
  return 1
}

# DGN-227 B6: manifest top-level 'net_new_skills' (optional array of skill ids)
# lets a pack DECLARE a skill as net-new (brand-new domain skill it brings in,
# no pre-existing instance bundle dir). The preflight rule-3 "instance bundle
# dir exists" requirement is waived for a skill that is either declared here OR
# whose payload provides a full directory (files beyond SKILL.md).
NET_NEW_SKILLS="$("$PACK_PY3" - "$MANIFEST" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
for s in d.get("net_new_skills") or []:
    print(s)
PYEOF
)"
_is_declared_net_new() { # _is_declared_net_new <skill-id> -- 0 when declared
  local id="$1" s
  while IFS= read -r s; do
    [[ -n "$s" && "$s" == "$id" ]] && return 0
  done <<< "$NET_NEW_SKILLS"
  return 1
}
# _skill_payload_is_full_dir <skill-id> -- 0 when the pack payload skills/<id>/
# carries files beyond SKILL.md (multi-file skill dir), i.e. eligible for the
# net-new install mode by shape.
_skill_payload_is_full_dir() {
  local id="$1" n
  n="$(find "$PKG_PAYLOAD/skills/$id" -type f 2>/dev/null | grep -cv '/SKILL\.md$' || true)"
  [[ "${n:-0}" -gt 0 ]]
}

[[ -n "$PACK_NAME" ]]     || _fail "manifest missing 'name': $MANIFEST"
[[ -n "$PKG_REF_SLUG" ]]  || _fail "manifest missing 'reference_slug': $MANIFEST"
[[ -n "$PKG_REF_ROOT" ]]  || _fail "manifest missing 'reference_root': $MANIFEST"
[[ -n "$CATEGORY_LINES" ]] || _fail "manifest declares no categories: $MANIFEST"
if has_cat agent_md_fragment; then
  [[ -n "$AGENT_MARKER" ]] || _fail "manifest declares agent_md_fragment but 'agent_md_marker' is empty: $MANIFEST"
fi
if has_cat agent_conf_fragment; then
  [[ -n "$CONF_MARKER" ]] || _fail "manifest declares agent_conf_fragment but 'agent_conf_marker' is empty: $MANIFEST"
fi

# The payload subdirectory is named after the manifest reference slug
PKG_PAYLOAD="$PACKAGE_DIR/$PKG_REF_SLUG"
PKG_LIB="$PACKAGE_DIR/lib"
PKG_SCRIPTS="$PKG_PAYLOAD/scripts"
KNOWLEDGE_SNAP="$PKG_SCRIPTS/knowledge-snapshot.sh"

# ---------- package reference identity rendering ------------------------------
# The package body was authored for the reference instance declared in the
# manifest. Launchd labels, plist filenames and reference paths inside the
# shipped payload are rewritten to the minted instance's slug and root at
# copy time so a second mint never collides with the reference instance.
_render_to() { # _render_to <src> <dst> -- copy with slug/root/home substitution
  # Order matters: instance root (absolute + tilde form) first, then the
  # remaining home prefix (when the manifest declares reference_home).
  local sed_args=()
  sed_args+=(-e "s|com\.telegram-skill-bot\.${PKG_REF_SLUG}\.|com.telegram-skill-bot.${SLUG}.|g")
  sed_args+=(-e "s|${PKG_REF_ROOT}|${ROOT}|g")
  if [[ -n "$PKG_REF_HOME" && "$PKG_REF_ROOT" == "$PKG_REF_HOME"/* ]]; then
    sed_args+=(-e "s|~${PKG_REF_ROOT#"$PKG_REF_HOME"}|${ROOT}|g")
    sed_args+=(-e "s|${PKG_REF_HOME}|${HOME}|g")
  fi
  # DGN-1845: atomic (temp sibling + rename) -- the dst may be live.
  atomic_write_cmd "$2" sed "${sed_args[@]}" "$1" \
    || _fail "render: write of $2 failed -- install aborted"
}

_render_basename() { # _render_basename <basename> -- slug-derived unit filename
  local b="$1"
  printf '%s\n' "${b//.${PKG_REF_SLUG}./.${SLUG}.}"
}

# _is_text_file <path> -- 0 when the file is text (render-eligible), 1 when
# binary (copy verbatim). Used by the B6 net-new skill directory install mode
# to decide render-vs-copy per file (spec B6: "text files -> render pipeline,
# binaries copied as-is"). NUL-byte probe (grep -Iq): a file with a NUL byte in
# the first chunk is treated as binary, matching git/POSIX text heuristics.
_is_text_file() {
  LC_ALL=C grep -Iq . "$1" 2>/dev/null || {
    # grep -I returns nonzero on binary OR empty file; an empty file is text.
    [[ -s "$1" ]] && return 1
  }
  return 0
}


# _preserve_register <relpath> -- idempotently add an instance-root-relative
# path to <root>/.claude/.dogany-preserve, tagged pack-owned, so update.sh
# section 3j does not clobber the pack-installed SKILL.md on framework
# refresh (DGN-402 layer 3 ownership; update.sh build_preserve_excludes
# already honors file-level entries). A pre-existing entry for the same path
# (pack-owned or hand-written) is left untouched.
_preserve_register() {
  local rel="$1"
  local pf="$ROOT/.claude/.dogany-preserve"
  if [[ ! -f "$pf" ]]; then
    {
      echo "# .dogany-preserve -- instance-local files update.sh must NOT refresh."
      echo "# One instance-root-relative path per line (trailing '/' = directory)."
      echo "# Lines tagged '# pack:<pack-id>' are managed by pack_install.sh."
    } > "$pf"
    _log "  created .claude/.dogany-preserve"
  fi
  if awk -v p="$rel" '{ line=$0; sub(/#.*/, "", line);
                        gsub(/^[[:space:]]+|[[:space:]]+$/, "", line);
                        if (line == p) found=1 }
                      END { exit found ? 0 : 1 }' "$pf"; then
    _log "  preserve entry already present: $rel (idempotent)"
  else
    # DGN-227 D1/P18: pack-owned tail tag '# pack:<id>' (machine-readable;
    # untagged lines = hand-written = untouchable).
    printf '%s  # pack:%s\n' "$rel" "$PACK_ID" >> "$pf"
    _log "  preserve-registered: $rel (pack:$PACK_ID)"
  fi
}

# DGN-227 B3/P25 installed-files ledger (LEDGER_DIR / _ledger_record /
# _ledger_finalize / _ledger_paths): hoisted above the pack-class dispatch
# (DGN-1526 R1) -- see "install ledger" there. Same functions, both paths.

# _preserve_reconcile -- DGN-227 D1/P18: verdict source REPLACED. Candidates =
# lines tail-tagged '# pack:<this-id>' (legacy '# pack-owned: <this-id>' lines
# are also candidates and get migrated/removed -- rehearsal note: legacy-tag
# handling is not specified by the spec, see OPEN QUESTIONS). Keep/remove =
# is the path in THIS install's ledger (config/packs/<id>.files). Untagged
# (hand-written) lines are never touched.
_preserve_reconcile() {
  local pf="$ROOT/.claude/.dogany-preserve"
  [[ -f "$pf" ]] || return 0
  local tmp removed=0 line path is_cand
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    is_cand=0
    case "$line" in
      *"# pack:${PACK_ID}") is_cand=1 ;;
      *"# pack-owned: ${PACK_ID} "*|*"# pack-owned: ${PACK_ID}") is_cand=1 ;;
    esac
    if [[ "$is_cand" -eq 1 ]]; then
      path="${line%%#*}"
      path="$(printf '%s' "$path" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      if ! _ledger_paths "$LEDGER_FILE" | grep -qxF "$path"; then
        _log "  preserve reconcile: removed stale pack entry (not in ledger): $path"
        removed=1
        continue
      fi
    fi
    printf '%s\n' "$line" >> "$tmp"
  done < "$pf"
  if [[ "$removed" -eq 1 ]]; then
    mv "$tmp" "$pf"
  else
    rm -f "$tmp"
  fi
}

# ---------- preflight (declared categories only) ------------------------------
if [[ "$MIGRATION" -eq 1 ]]; then
  _log "preflight: pack=$PACK_ID root=$ROOT slug=$SLUG path=migration peer=$PEER_ROOT"
else
  _log "preflight: pack=$PACK_ID root=$ROOT slug=$SLUG path=fresh (no peer)"
fi
_log "preflight: manifest=$MANIFEST ref_slug=$PKG_REF_SLUG"

fail=0
if [[ "$MIGRATION" -eq 1 ]]; then
  [[ -d "$PEER_ROOT" ]] || { _log "PREFLIGHT FAIL: --migrate-from peer root not found: $PEER_ROOT"; fail=1; }
fi
[[ -d "$ROOT" ]]        || { _log "PREFLIGHT FAIL: root not found: $ROOT"; fail=1; }
[[ -d "$ROOT/bridge" ]] || { _log "PREFLIGHT FAIL: not a minted instance (no bridge/): $ROOT"; fail=1; }
# DGN-773 R5 2.1-orphan guard: PROFILE.md real + AGENT.md ALSO a real file
# (not the compat symlink) means the AGENT.md/PROFILE.md compat link was
# removed (2.1) and something then raw-wrote a fresh AGENT.md -- an orphan
# identity file the resolver would never target. Fail-closed rather than
# silently diverging two personas.
if [[ -f "$ROOT/PROFILE.md" && -f "$ROOT/AGENT.md" && ! -L "$ROOT/AGENT.md" ]]; then
  _log "PREFLIGHT FAIL: both PROFILE.md and a REAL (non-symlink) AGENT.md exist at $ROOT -- orphaned identity file (compat link missing); resolve manually before installing"
  fail=1
fi
[[ -d "$PACKAGE_DIR" ]] || { _log "PREFLIGHT FAIL: package_dir not found: $PACKAGE_DIR"; fail=1; }
[[ -d "$PKG_PAYLOAD" ]] || { _log "PREFLIGHT FAIL: package payload subdir ($PKG_REF_SLUG/) not found: $PKG_PAYLOAD"; fail=1; }
[[ -f "$ROOT/.claude/settings.json" ]] || { _log "PREFLIGHT FAIL: settings.json not found: $ROOT/.claude/settings.json"; fail=1; }
# Sibling check: the hook wire target (DGN-1250 landing condition 3).
_settings_local_preflight "$ROOT" || { _log "PREFLIGHT FAIL: $_SETTINGS_LOCAL_VERDICT"; fail=1; }
[[ -n "${PACK_PY3:-}" ]] || { _log "PREFLIGHT FAIL: no Python 3.11+ interpreter resolved"; fail=1; }

_preflight_cat() { # _preflight_cat <name> <check-type> <path>
  local name="$1" ctype="$2" path="$3" ok=1
  has_cat "$name" || return 0
  case "$ctype" in
    dir)  [[ -d "$path" ]] && ok=0 ;;
    file) [[ -f "$path" ]] && ok=0 ;;
    exec) [[ -x "$path" ]] && ok=0 ;;
    glob) compgen -G "$path" >/dev/null 2>&1 && ok=0 ;;
  esac
  if [[ "$ok" -ne 0 ]]; then
    if cat_required "$name"; then
      _log "PREFLIGHT FAIL: required category '$name' payload missing: $path"
      fail=1
    else
      _log "preflight: optional category '$name' payload missing ($path) -- will skip"
    fi
  fi
}

_preflight_cat lib                 dir  "$PKG_LIB"
_preflight_cat routines            dir  "$PKG_PAYLOAD/routines"
_preflight_cat plists              glob "$PKG_PAYLOAD/routines/*.plist"
_preflight_cat prompts             dir  "$PKG_PAYLOAD/routines/prompts"
_preflight_cat agent_conf_fragment file "$PKG_PAYLOAD/config/agent.conf.add"
_preflight_cat triggers            file "$PKG_PAYLOAD/config/triggers.yaml"
_preflight_cat db_migrations       dir  "$PKG_PAYLOAD/database/migrations"
_preflight_cat skills              dir  "$PKG_PAYLOAD/skills"
_preflight_cat agent_md_fragment   file "$PKG_PAYLOAD/AGENT.md.add"
_preflight_cat scripts             dir  "$PKG_SCRIPTS"
_preflight_cat knowledge_snapshot  exec "$KNOWLEDGE_SNAP"

# ---- knowledge wiring preflight (DGN-402 spec v2.1 S1/S2) -------------------
# Rule 1: manifest 'knowledge' object and knowledge_snapshot category must
#         appear together (half declaration = manifest error).
if has_cat knowledge_snapshot && [[ "$KNOWLEDGE_DECL" -eq 0 ]]; then
  _log "PREFLIGHT FAIL: category knowledge_snapshot declared without a manifest 'knowledge' object (half declaration)"
  fail=1
fi
if [[ "$KNOWLEDGE_DECL" -eq 1 ]]; then
  if ! has_cat knowledge_snapshot; then
    _log "PREFLIGHT FAIL: manifest 'knowledge' object declared without the knowledge_snapshot category (half declaration)"
    fail=1
  fi
  # Rule 2: knowledge_snapshot is valid only together with the scripts
  #         category -- STEP 6 runs the snapshot script STEP 2f installs.
  if ! has_cat scripts; then
    _log "PREFLIGHT FAIL: knowledge declared but 'scripts' category missing (STEP 6 runs the script STEP 2f copies to <root>/scripts/)"
    fail=1
  fi
  [[ -n "$KNOW_WAREHOUSE" ]] || { _log "PREFLIGHT FAIL: knowledge.warehouse is empty"; fail=1; }
  # Rule 3: consumer_skills nonempty; each skill must exist in the pack
  #         payload skills/. The "instance skills-bundle dir exists" requirement
  #         (DGN-402) is WAIVED (DGN-227 B6) when the skill is net-new -- either
  #         declared in manifest net_new_skills OR the payload provides a full
  #         directory (files beyond SKILL.md). STEP 7 net-new mode installs the
  #         whole directory in that case (no pre-existing bundle dir needed).
  if [[ -z "$KNOW_CONSUMER_LINES" ]]; then
    _log "PREFLIGHT FAIL: knowledge.consumer_skills is empty"
    fail=1
  else
    while IFS=$'\t' read -r _ck _cd; do
      [[ -n "$_ck" ]] || continue
      [[ -f "$PKG_PAYLOAD/skills/$_ck/SKILL.md" ]] || { _log "PREFLIGHT FAIL: consumer skill '$_ck' not in pack payload skills/"; fail=1; }
      if [[ ! -d "$ROOT/.claude/skills-bundle/$_ck" ]]; then
        if _is_declared_net_new "$_ck" || _skill_payload_is_full_dir "$_ck"; then
          _log "preflight: consumer skill '$_ck' net-new (no instance bundle dir) -- STEP 7 will install the full directory (B6)"
        else
          _log "PREFLIGHT FAIL: consumer skill '$_ck' has no instance skills-bundle dir and is not net-new (declare in net_new_skills or ship a full payload dir): $ROOT/.claude/skills-bundle/$_ck"
          fail=1
        fi
      fi
    done <<< "$KNOW_CONSUMER_LINES"
  fi
  # Rule 4: turns nonempty; type T1/T2/T3 only; home is an instance-root-
  #         relative pack artifact path (existence is enforced post-install
  #         by the STEP 7c gate G4 on <root>/<home>).
  if [[ -z "$KNOW_TURN_LINES" ]]; then
    _log "PREFLIGHT FAIL: knowledge.turns is empty"
    fail=1
  else
    while IFS=$'\t' read -r _tt _th; do
      [[ -n "$_tt$_th" ]] || continue
      case "$_tt" in
        T1|T2|T3) : ;;
        *) _log "PREFLIGHT FAIL: knowledge.turns type must be T1/T2/T3 (got '$_tt')"; fail=1 ;;
      esac
      [[ -n "$_th" ]] || { _log "PREFLIGHT FAIL: knowledge.turns entry has empty home"; fail=1; }
      case "$_th" in
        /*|*..*) _log "PREFLIGHT FAIL: knowledge.turns home must be an instance-root-relative pack artifact path (got '$_th')"; fail=1 ;;
      esac
    done <<< "$KNOW_TURN_LINES"
  fi
fi

[[ "$fail" -eq 0 ]] || exit 1

if [[ "$DRY" -eq 1 ]]; then
  _log "dry-run: preflight OK"
  _log "dry-run: would run full pack-install chain for pack=$PACK_ID -> $ROOT"
  _log "dry-run: package_dir=$PACKAGE_DIR"
  _log "dry-run: declared categories: $(printf '%s\n' "$CATEGORY_LINES" | awk -F'\t' '{printf "%s ", $1}')"
  _log "dry-run: domain_seed declared: $DOMAIN_SEED_DECL"
  if [[ "$MIGRATION" -eq 1 ]]; then
    _log "dry-run: path=migration peer=$PEER_ROOT (peer keys appended, domain seed=pending_data)"
  else
    _log "dry-run: path=fresh (no peer keys, domain seed=ready)"
  fi
  if [[ -n "$INSTANCE_ROOT" ]]; then
    _log "dry-run: instance-root=$INSTANCE_ROOT (minting_state record enabled)"
  else
    _log "dry-run: instance-root NOT supplied -- step 11 minting_state record would SKIP (explicit)"
  fi
  _log "dry-run: bridge-start=caller-owned (--no-start compatibility flag=$NO_START)"
  echo "[pack_install] DRY-RUN OK -- no writes"
  exit 0
fi

# ---------- ensure log dir exists -------------------------------------------
mkdir -p "$LOG_DIR"
_log "=== pack-install START pack=$PACK_ID slug=$SLUG root=$ROOT ==="

# ---------- NM3: payload checksum verification GATE -------------------------
# Shared with the kit/pack contract-class path -- see _nm3_verify above for the
# gate's contract (present => verify, mismatch/missing => FATAL; absent =>
# loud WARN + continue). Runs here, after the dry-run exit and before any
# payload write, exactly as it always has on this path. DGN-1549: the refslug
# dir is the installed payload subtree, so it scopes the unsealed-extras scan
# on the work-tree lane (same reasoning as the contract-path call).
_nm3_verify "$PACKAGE_SRC_DIR" "$PKG_REF_SLUG"

# ---------- compat-lint: install-side contract gate (DGN-803 LS-3) -----------
# Ubypassable final gate: verifies pack manifest + payload satisfy the
# framework contract before any payload is written to the instance root.
# Failure here aborts the install (FAIL loudly, exit 1).
_COMPAT_LINT="$SCRIPT_DIR/compat-lint.sh"
if [[ -f "$_COMPAT_LINT" ]]; then
  _FW_VERSION=""
  if [[ -f "$REPO_DIR/VERSION" ]]; then
    _FW_VERSION="$(tr -d '[:space:]' < "$REPO_DIR/VERSION")"
  fi
  if [[ -z "$_FW_VERSION" ]]; then
    _fail "compat-lint: cannot determine framework VERSION (VERSION file missing at $REPO_DIR/VERSION)"
  fi
  _log "compat-lint: running install-side contract gate (framework=$_FW_VERSION pack=$PACKAGE_DIR)"
  _lint_out=""
  if ! _lint_out="$(bash "$_COMPAT_LINT" \
      --pack-dir "$PACKAGE_DIR" \
      --framework-version "$_FW_VERSION" \
      --catalog "$CATALOG" \
      --install-side 2>&1)"; then
    while IFS= read -r _ll; do _log "  $_ll"; done <<< "$_lint_out"
    _fail "compat-lint: install-side contract gate FAILED -- install aborted (DGN-803 LS-3)"
  fi
  while IFS= read -r _ll; do _log "  $_ll"; done <<< "$_lint_out"
  _log "compat-lint: install-side gate PASS"
else
  # DGN-1682: same strict-mode fail-closed as the kit/pack contract path.
  if [[ "${DOGANY_STRICT_GATES:-0}" == "1" ]]; then
    _fail "compat-lint: compat-lint.sh not found at $_COMPAT_LINT -- install-side gate cannot run under DOGANY_STRICT_GATES=1, refusing (fail-closed, DGN-1682)"
  fi
  _log "compat-lint: WARN -- compat-lint.sh not found at $_COMPAT_LINT -- gate SKIPPED (install DGN-803 LS-3 not deployed)"
fi

# DGN-227 D2 note: the --upgrade stale-removal phase (ledger diff + bootout +
# NM3 backup + removal + ledger re-record) lives AFTER the apply steps, next
# to _preserve_reconcile -- the old ledger stays untouched on disk until
# _ledger_finalize there, so the diff source survives the whole apply pass.
if [[ "${UPGRADE:-0}" -eq 1 ]]; then
  _log "=== pack-install UPGRADE pack=$PACK_ID slug=$SLUG root=$ROOT ==="
fi

# ---------- DGN-1460: install evidence -- in-progress marker ----------------
# Same contract as the kit/pack path: a durable run record lands BEFORE the
# first payload byte, and only evidence_complete (right before the ledger
# upsert below) promotes it. This legacy lane declares no anchors -- its
# delivered set is the .files ledger written mid-run -- so the receipt here
# carries the run lifecycle (started/completed), which is exactly what the
# DGN-1460 orphan stamps could not prove.
evidence_begin "$ROOT" "$PACK_ID" "$RECORD_VERSION" "legacy" "" "$EVIDENCE_SOURCE" \
  || _fail "install evidence: in-progress receipt write failed -- refusing to mutate the instance without a durable run record (DGN-1460)"
_log "install evidence: in-progress marker issued (config/packs/$PACK_ID.receipt.inprogress)"

# ---------- STEP 2: package copy (declared categories only) ------------------
_log "step 2: package copy"

# Every copy below lands atomically (lib/atomic_write.sh, DGN-1845): the
# targets are live modules/scripts a running turn may be reading.

# 2a. lib/ -> routines/lib/
if has_cat lib && [[ -d "$PKG_LIB" ]]; then
  mkdir -p "$ROOT/routines/lib"
  if [[ -n "$LIB_FILES" ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      if [[ -f "$PKG_LIB/$f" ]]; then
        atomic_cp "$PKG_LIB/$f" "$ROOT/routines/lib/$f" || _fail "step 2: copy failed for routines/lib/$f -- install aborted"
        _ledger_record "routines/lib/$f"
        _preserve_register "routines/lib/$f"
        _log "  copied lib/$f -> routines/lib/"
      else
        _log "  WARN: manifest lib file not in package: lib/$f (skipping)"
      fi
    done <<< "$LIB_FILES"
  else
    for f in "$PKG_LIB/"*.py; do
      [[ -e "$f" ]] || continue
      atomic_cp "$f" "$ROOT/routines/lib/$(basename "$f")" || _fail "step 2: copy failed for routines/lib/$(basename "$f") -- install aborted"
      _ledger_record "routines/lib/$(basename "$f")"
      _preserve_register "routines/lib/$(basename "$f")"
      _log "  copied lib/$(basename "$f") -> routines/lib/"
    done
  fi
else
  has_cat lib || _log "  category lib not declared -- skipping"
fi

# 2b. routines/ -> routines/ (sh + py files)
if has_cat routines && [[ -d "$PKG_PAYLOAD/routines" ]]; then
  for f in "$PKG_PAYLOAD/routines/"*.sh; do
    [[ -e "$f" ]] || continue
    atomic_cp "$f" "$ROOT/routines/$(basename "$f")" || _fail "step 2: copy failed for routines/$(basename "$f") -- install aborted"
    chmod +x "$ROOT/routines/$(basename "$f")"
    _ledger_record "routines/$(basename "$f")"
    _preserve_register "routines/$(basename "$f")"
    _log "  copied routines/$(basename "$f")"
  done
  for f in "$PKG_PAYLOAD/routines/"*.py; do
    [[ -e "$f" ]] || continue
    atomic_cp "$f" "$ROOT/routines/$(basename "$f")" || _fail "step 2: copy failed for routines/$(basename "$f") -- install aborted"
    _ledger_record "routines/$(basename "$f")"
    _preserve_register "routines/$(basename "$f")"
    _log "  copied routines/$(basename "$f")"
  done
else
  has_cat routines || _log "  category routines not declared -- skipping"
fi

# 2c. plists + plists.defer -- RENDERED, not copied (DGN-284 #1): launchd
#     label + filename derive from the minted slug; package-reference paths
#     rewritten to $ROOT.
if has_cat plists; then
  for f in "$PKG_PAYLOAD/routines/"*.plist; do
    [[ -e "$f" ]] || continue
    dst_base="$(_render_basename "$(basename "$f")")"
    _render_to "$f" "$ROOT/routines/$dst_base"
    _ledger_record "routines/$dst_base"
    _preserve_register "routines/$dst_base"
    _log "  rendered routines/$dst_base (slug-derived label + instance paths)"
  done
  # DGN-227 MINOR-5: defer-merge policy = merge-append (NOT clobber). A pack
  # that bundles its own plists.defer must NOT overwrite the instance framework
  # defer manifest (which stages the generic-brief units). Instead, the pack's
  # rendered defer ENTRIES are appended to the existing instance defer, both
  # preserved. Substitution is _render_to (same slug/root rewrite as the plist
  # filenames), keeping the output format-consistent with DGN-417's
  # telegram-agent->agent-name substitution (no literal telegram-agent
  # leftover). Duplicate basenames (already in the instance defer) are skipped.
  # deferral manifest basenames must match the rendered plist filenames.
  if [[ -f "$PKG_PAYLOAD/routines/plists.defer" ]]; then
    _pack_defer="$(mktemp)"
    _render_to "$PKG_PAYLOAD/routines/plists.defer" "$_pack_defer"
    _inst_defer="$ROOT/routines/plists.defer"
    if [[ ! -f "$_inst_defer" ]]; then
      # No framework defer present -- the pack defer becomes the manifest.
      atomic_cp "$_pack_defer" "$_inst_defer" || _fail "step 2: copy failed for routines/plists.defer -- install aborted"
      _log "  installed routines/plists.defer (no prior framework defer -- pack defer adopted)"
    else
      _appended=0
      _pack_marker="# --- pack:$PACK_ID defer entries (DGN-227 MINOR-5 merge-append) ---"
      while IFS= read -r _de || [[ -n "$_de" ]]; do
        # skip blank + comment lines from the pack defer body
        case "$_de" in ''|'#'*) continue ;; esac
        # dedup: entry already present (any non-comment line) -> skip
        if grep -qxF "$_de" "$_inst_defer"; then
          _log "  defer merge: entry already present, skipping: $_de"
          continue
        fi
        if [[ "$_appended" -eq 0 ]]; then
          # append the section marker once, before the first new entry
          if ! grep -qxF "$_pack_marker" "$_inst_defer"; then
            printf '%s\n' "$_pack_marker" >> "$_inst_defer"
          fi
          _appended=1
        fi
        printf '%s\n' "$_de" >> "$_inst_defer"
        _log "  defer merge: appended pack entry: $_de"
      done < "$_pack_defer"
      [[ "$_appended" -eq 1 ]] || _log "  defer merge: all pack entries already present (idempotent, no change)"
    fi
    rm -f "$_pack_defer"
    _ledger_record "routines/plists.defer"
    _preserve_register "routines/plists.defer"
    _log "  merged routines/plists.defer (framework entries preserved + pack entries appended)"
  fi
else
  _log "  category plists not declared -- skipping"
fi

# 2d. prompts/
if has_cat prompts && [[ -d "$PKG_PAYLOAD/routines/prompts" ]]; then
  mkdir -p "$ROOT/routines/prompts"
  # DGN-1845: per-file atomic copy (was one `cp -rf` of the tree in place).
  while IFS= read -r _pf; do
    _rel="routines/prompts/${_pf#"$PKG_PAYLOAD/routines/prompts/"}"
    mkdir -p "$(dirname "$ROOT/$_rel")"
    atomic_cp "$_pf" "$ROOT/$_rel" || _fail "step 2: copy failed for $_rel -- install aborted"
    _ledger_record "$_rel"
    _preserve_register "$_rel"
  done < <(find "$PKG_PAYLOAD/routines/prompts" -type f)
  _log "  copied routines/prompts/"
else
  has_cat prompts || _log "  category prompts not declared -- skipping"
fi

# 2e. database/migrations/ -> database/migrations/
if has_cat db_migrations && [[ -d "$PKG_PAYLOAD/database/migrations" ]]; then
  mkdir -p "$ROOT/database/migrations"
  for f in "$PKG_PAYLOAD/database/migrations/"*.sql; do
    [[ -e "$f" ]] || continue
    atomic_cp "$f" "$ROOT/database/migrations/$(basename "$f")" || _fail "step 2: copy failed for database/migrations/$(basename "$f") -- install aborted"
    _ledger_record "database/migrations/$(basename "$f")"
    _log "  copied database/migrations/$(basename "$f")"
  done
else
  has_cat db_migrations || _log "  category db_migrations not declared -- skipping"
fi

# 2f. scripts/ -> scripts/
if has_cat scripts && [[ -d "$PKG_SCRIPTS" ]]; then
  mkdir -p "$ROOT/scripts"
  for f in "$PKG_SCRIPTS/"*.sh; do
    [[ -e "$f" ]] || continue
    atomic_cp "$f" "$ROOT/scripts/$(basename "$f")" || _fail "step 2: copy failed for scripts/$(basename "$f") -- install aborted"
    chmod +x "$ROOT/scripts/$(basename "$f")"
    _ledger_record "scripts/$(basename "$f")"
    _log "  copied scripts/$(basename "$f")"
  done
else
  has_cat scripts || _log "  category scripts not declared -- skipping"
fi

# 2g. config/triggers.yaml
if has_cat triggers && [[ -f "$PKG_PAYLOAD/config/triggers.yaml" ]]; then
  mkdir -p "$ROOT/config"
  atomic_cp "$PKG_PAYLOAD/config/triggers.yaml" "$ROOT/config/triggers.yaml" || _fail "step 2: copy failed for config/triggers.yaml -- install aborted"
  _ledger_record "config/triggers.yaml"
  _log "  copied config/triggers.yaml"
else
  has_cat triggers || _log "  category triggers not declared -- skipping"
fi

_log "step 2: package copy done"

# ---------- STEP 3: agent.conf fragment append (idempotent) -----------------
# DGN-1467: marker-pair mechanics live in _conf_fragment_apply (shared with
# the contract-class path above); this call site keeps its position, order
# and argument meaning (DGN-1018 §5-B no-regression). PEER_KEYS_RE is
# defined next to the shared functions (hoisted, same value).
if has_cat agent_conf_fragment; then
  _log "step 3: agent.conf append"

  CONF_ADD="$PKG_PAYLOAD/config/agent.conf.add"
  AGENT_CONF="$ROOT/config/agent.conf"
  MARKER="$CONF_MARKER"

  if [[ -f "$CONF_ADD" ]]; then
    _CONF_BODY="$(mktemp)"
    if [[ "$MIGRATION" -eq 1 ]]; then
      {
        # strip comment-only header lines; point peer keys at the actual peer
        grep -v '^#' "$CONF_ADD" | grep -v '^$' \
          | sed -e "s|^L1_DB=.*|L1_DB=$PEER_ROOT/database/lifekit.db|" \
                -e "s|^HANDOFF_PEER_AG=.*|HANDOFF_PEER_AG=$PEER_ROOT|" \
                -e "s|^MIGRATION_PEER=.*|MIGRATION_PEER=$PEER_ROOT|" || true
        # DGN-227 E2-1/P24: the migration discriminator key is ALWAYS written
        # on the migration path, even when the fragment does not carry it.
        if ! grep -Eq '^MIGRATION_PEER=' "$CONF_ADD"; then
          echo "MIGRATION_PEER=$PEER_ROOT"
        fi
      } > "$_CONF_BODY"
      _conf_fragment_apply "step 3" "$AGENT_CONF" "$_CONF_BODY" "$PACK_ID" "$MARKER" legacy \
        "  appended config/agent.conf.add (migration path; peer=$PEER_ROOT; MIGRATION_PEER set)"
    else
      {
        echo "# fresh/standalone mint (DGN-284/DGN-227): migration-family keys"
        echo "# (L1_DB / L1_EXPECTED_USER_VERSION / HANDOFF_PEER_AG / MIGRATION_PEER) intentionally omitted"
        # keep any future non-migration keys the fragment may carry
        grep -v '^#' "$CONF_ADD" | grep -v '^$' | grep -Ev "$PEER_KEYS_RE" || true
      } > "$_CONF_BODY"
      _conf_fragment_apply "step 3" "$AGENT_CONF" "$_CONF_BODY" "$PACK_ID" "$MARKER" legacy \
        "  appended config/agent.conf.add (fresh path; migration keys omitted)"
    fi
    rm -f "$_CONF_BODY"
  else
    _log "  no agent.conf.add in package (skipping)"
  fi

  _log "step 3: agent.conf done"
else
  _log "step 3: agent.conf append SKIPPED (category agent_conf_fragment not declared)"
fi

# ---------- STEP 4: W01 ledger apply CLI ------------------------------------
# Runs only when the pack actually ships lib/ledger.py (declaration-driven:
# no generic hard requirement on ledger machinery).
if has_cat lib && [[ -f "$PKG_LIB/ledger.py" ]]; then
  _log "step 4: W01 ledger apply (ledger.py apply)"

  LEDGER_PY="$ROOT/routines/lib/ledger.py"
  DB="$ROOT/database/lifekit.db"

  if [[ -f "$LEDGER_PY" && -f "$DB" ]]; then
    cd "$ROOT"
    "$PACK_PY3" "$LEDGER_PY" apply --db "$DB" 2>&1 | while IFS= read -r line; do _log "  ledger: $line"; done
    _log "step 4: W01 apply done"
  else
    [[ -f "$LEDGER_PY" ]] || _fail "step 4: ledger.py not found at $LEDGER_PY (package copy step 2 failed?)"
    [[ -f "$DB" ]] || _fail "step 4: lifekit.db not found at $DB (fresh mint incomplete?)"
  fi
else
  _log "step 4: ledger apply SKIPPED (pack ships no lib/ledger.py)"
fi

# ---------- STEP 5: hook wiring (settings.local.json -- idempotent edit) ----
# Declaration-driven (DGN-1496): wires exactly what the manifest hooks[]
# declares -- the installer knows neither filename nor hook event. The old
# form pinned both (routines/ledger-inject.py + UserPromptSubmit); that case
# is now expressed AS a declaration in the shipping pack's manifest, not as a
# surviving hardcoded branch. Legacy surface = flat routines/*.py|*.sh (what
# STEP 2 above actually delivers). Shared helpers: _hooks_declared /
# _hooks_validate / _hooks_wire (defined next to the delivery census).
# SETTINGS stays the framework-owned file -- STEP 9 below stamps the model
# there, which is correct, it is the framework's own key. Only the HOOK
# target moved to the instance-owned sibling (DGN-1250 / DGN-1665).
SETTINGS="$ROOT/.claude/settings.json"
SETTINGS_LOCAL="$ROOT/.claude/settings.local.json"
if _hooks_declared "$MANIFEST"; then
  _log "step 5: declared hook wiring (manifest hooks[])"
  _hooks_validate "step 5" "$MANIFEST" "$PKG_PAYLOAD" flat
  _hooks_wire "step 5" "$SETTINGS_LOCAL" "$ROOT" "$_HOOK_LINES"
  _log "step 5: hook wiring done"
elif [[ -f "$PKG_PAYLOAD/routines/ledger-inject.py" ]]; then
  # Migration tripwire, NOT a wiring branch: before DGN-1496 this payload
  # file alone got silently wired to UserPromptSubmit. Wiring it here would
  # keep two paths doing one job; skipping silently would un-wire a hook the
  # pack relies on and defer the surprise to session time. Fail loudly with
  # the one-line fix instead. Remove together with the legacy install path.
  _fail "step 5: payload ships routines/ledger-inject.py but the manifest declares no hooks -- hook wiring is declaration-driven (DGN-1496). Declare it in pack-manifest.json: \"hooks\": [ { \"event\": \"UserPromptSubmit\", \"script\": \"routines/ledger-inject.py\" } ]"
else
  _log "step 5: hook wiring SKIPPED (manifest declares no hooks)"
fi

# ---------- STEP 6: knowledge snapshot --------------------------------------
# DGN-227 B5: source resolution priority (frozen-snapshot delivery channel):
#   (1) a bundled frozen snapshot at <package_dir>/<reference_slug>/knowledge/
#       (customer-machine path) -- inject it as the snapshot source so a pack
#       ships its warehouse to other machines; the publisher-local path is not
#       required to exist. This is the B5 delivery channel (F1 resolution).
#   (2) absent -> fall back to the manifest knowledge.source publisher-local
#       path (same-machine pilot / dev scenario, DGN-402 behavior preserved).
# The snapshot script call convention (idempotent rsync + pin record +
# instance user-data exclusion) is unchanged -- only the source path differs.
if has_cat knowledge_snapshot; then
  _log "step 6: knowledge snapshot"

  SNAPSHOT_SH="$ROOT/scripts/knowledge-snapshot.sh"
  PKG_KNOWLEDGE="$PKG_PAYLOAD/knowledge"
  SNAP_SOURCE=""
  if [[ -n "$KNOW_WAREHOUSE" && -d "$PKG_KNOWLEDGE/$KNOW_WAREHOUSE" ]]; then
    SNAP_SOURCE="$PKG_KNOWLEDGE/$KNOW_WAREHOUSE"
    _log "  snapshot source (bundled frozen channel, B5): $SNAP_SOURCE"
  elif [[ -n "$KNOW_SOURCE" ]]; then
    SNAP_SOURCE="$KNOW_SOURCE"
    _log "  snapshot source (manifest knowledge.source, publisher-local fallback): $SNAP_SOURCE"
  fi
  if [[ -x "$SNAPSHOT_SH" ]]; then
    # DGN-1008: the `| while read` log pump SWALLOWED the script's exit code --
    # a snapshot refusal (e.g. the destination-ahead guard) used to look like a
    # successful step with an error merely logged. Capture PIPESTATUS[0] and
    # surface it loudly. Deliberately NOT a hard _fail: the guard's whole point
    # is that nothing was written, so the install may continue -- but it must
    # not continue SILENTLY, and the ledger must not record a delivery that
    # never happened.
    bash "$SNAPSHOT_SH" "$ROOT" ${SNAP_SOURCE:+"$SNAP_SOURCE"} 2>&1 | while IFS= read -r line; do _log "  snapshot: $line"; done
    _SNAP_RC="${PIPESTATUS[0]}"
    if [[ "$_SNAP_RC" -ne 0 ]]; then
      _log "  WARN: knowledge-snapshot.sh exited $_SNAP_RC -- warehouse NOT delivered this run (see snapshot: lines above)"
      _log "step 6: knowledge snapshot FAILED (rc=$_SNAP_RC) -- no ledger record written"
    else
      # DGN-227 B3: the warehouse root DIRECTORY is the ledger unit for
      # knowledge (per-file churn is snapshot-internal).
      [[ -n "$KNOW_WAREHOUSE" ]] && _ledger_record "knowledge/$KNOW_WAREHOUSE/"
      _log "step 6: knowledge snapshot done"
    fi
  else
    _log "  WARN: knowledge-snapshot.sh not found at $ROOT/scripts/ -- skipping (pack may not require it)"
  fi
else
  _log "step 6: knowledge snapshot SKIPPED (category knowledge_snapshot not declared)"
fi

# ---------- STEP 7: refined skills install (SKILL.md + symlinks) ------------
if has_cat skills && [[ -d "$PKG_PAYLOAD/skills" ]]; then
  _log "step 7: refined skills install"

  for skill_dir in "$PKG_PAYLOAD/skills/"*/; do
    skill_id="$(basename "$skill_dir")"
    src_skill_md="$skill_dir/SKILL.md"
    bundle_dir="$ROOT/.claude/skills-bundle/$skill_id"
    link_path="$ROOT/.claude/skills/$skill_id"

    if [[ ! -f "$src_skill_md" ]]; then
      _log "  WARN: no SKILL.md in package skills/$skill_id -- skipping"
      continue
    fi

    if [[ -d "$bundle_dir" ]]; then
      # ---- refine mode (DGN-402): instance bundle dir exists -> render the
      #      SKILL.md only (single-file refine of an existing framework skill).
      # DGN-402: plain cp replaced by the render pipeline -- reference-identity
      # substitution (slug/root/home) + mint-token substitution. Unrendered
      # token residue is a hard FAIL at the STEP 7c gate (G4).
      _render_to "$src_skill_md" "$bundle_dir/SKILL.md"
      _subst_mint_tokens "$bundle_dir/SKILL.md"
      _log "  installed .claude/skills-bundle/$skill_id/SKILL.md (rendered, refine mode)"
      _ledger_record ".claude/skills-bundle/$skill_id/SKILL.md"

      # DGN-227 D1/P18 (widens DGN-402 layer 3): EVERY pack file landing in an
      # update.sh allowlist-managed zone (.claude/skills-bundle here) is
      # preserve-registered pack-owned -- not only consumer skills. Pack
      # reinstall still overwrites (preserve binds update.sh only).
      _preserve_register ".claude/skills-bundle/$skill_id/SKILL.md"
    else
      # ---- net-new mode (DGN-227 B6): a brand-new multi-file domain skill the
      #      pack brings in. Install the WHOLE payload skill directory into
      #      .claude/skills-bundle/<id>/. Text files go through the render
      #      pipeline (reference-identity + mint-token subst); binaries are
      #      copied verbatim. Each installed file is ledger-recorded and
      #      preserve-registered pack-owned (D1) so it survives framework
      #      refresh AND is reconciled on upgrade. G4 (STEP 7c) applies the
      #      unrendered-token gate to every net-new file.
      _log "  net-new skill directory: $skill_id (bundle dir absent -> full install)"
      mkdir -p "$bundle_dir"
      while IFS= read -r _sf; do
        _rel="${_sf#"$skill_dir"}"           # path relative to the skill dir root
        _rel="${_rel#/}"
        _dst="$bundle_dir/$_rel"
        mkdir -p "$(dirname "$_dst")"
        if _is_text_file "$_sf"; then
          _render_to "$_sf" "$_dst"
          _subst_mint_tokens "$_dst"
          # preserve the source executable bit through the render (render writes
          # a fresh file via sed, dropping mode)
          [[ -x "$_sf" ]] && chmod +x "$_dst"
          _log "    rendered .claude/skills-bundle/$skill_id/$_rel"
        else
          atomic_cp "$_sf" "$_dst" || _fail "skills: copy failed for .claude/skills-bundle/$skill_id/$_rel -- install aborted"
          _log "    copied (binary) .claude/skills-bundle/$skill_id/$_rel"
        fi
        _ledger_record ".claude/skills-bundle/$skill_id/$_rel"
        _preserve_register ".claude/skills-bundle/$skill_id/$_rel"
      done < <(find "$skill_dir" -type f)
      _log "  installed net-new skill .claude/skills-bundle/$skill_id/ (full directory)"
    fi

    # ensure symlink (template wiring: skills/ -> skills-bundle/)
    mkdir -p "$ROOT/.claude/skills"
    if [[ -L "$link_path" ]]; then
      current_target="$(readlink "$link_path")"
      expected_target="../skills-bundle/$skill_id"
      if [[ "$current_target" != "$expected_target" ]]; then
        ln -sfn "$expected_target" "$link_path"
        _log "  re-linked .claude/skills/$skill_id -> ../skills-bundle/$skill_id"
      else
        _log "  symlink .claude/skills/$skill_id already correct (idempotent)"
      fi
    else
      # DGN-1798: fail-closed -- a real dir/file here is owner-made; never remove it.
      [[ -e "$link_path" ]] && _fail "step 7: $link_path is a REAL directory, not a symlink -- refusing to touch it (owner customization). Move or rename it (or delete it yourself if unwanted), then re-run the install"
      ln -sfn "../skills-bundle/$skill_id" "$link_path"
      _log "  linked .claude/skills/$skill_id -> ../skills-bundle/$skill_id"
    fi
  done
  _log "step 7: skills install done"
else
  _log "step 7: skills install SKIPPED (category skills not declared)"
fi

# ---------- DGN-227 B3/D2: ledger finalize + --upgrade stale removal --------
# Rehearsal ordering note (OPEN QUESTION): spec D2 orders the phases
# remove -> apply -> re-record; here the removal diff runs AFTER apply using
# the freshly recorded ledger (old-ledger snapshot taken first). End state is
# identical (a stale path can never equal a new path), but it deviates from
# the spec's literal phase order -- escalated, see the rehearsal report.
OLD_LEDGER_SNAP=""
if [[ "$UPGRADE" -eq 1 ]]; then
  if [[ -f "$LEDGER_FILE" ]]; then
    OLD_LEDGER_SNAP="$(mktemp)"
    cp "$LEDGER_FILE" "$OLD_LEDGER_SNAP"
  else
    # legacy ledger-less install (pre-DGN-227): removal diff has no source.
    _log "  WARN: --upgrade with NO prior ledger (legacy install) -- stale-removal phase SKIPPED (loud, not silent); the ledger recorded now arms removal semantics for the NEXT upgrade"
  fi
fi

_ledger_finalize

if [[ "$UPGRADE" -eq 1 && -n "$OLD_LEDGER_SNAP" ]]; then
  _log "  upgrade: stale diff (old ledger vs new payload set)"
  UPG_BACKUP_DIR="$ROOT/files/_archive/pack-upgrade-$PACK_ID-$(date +%Y%m%d-%H%M%S)"
  while IFS= read -r stale; do
    [[ -n "$stale" ]] || continue
    if _ledger_paths "$LEDGER_FILE" | grep -qxF "$stale"; then
      continue   # still in the new target set -- not stale
    fi
    if [[ ! -e "$ROOT/$stale" ]]; then
      _log "  upgrade: stale ledger entry has no file on disk: $stale (ledger drift -- skipping)"
      continue
    fi
    # (a) plists get a launchd bootout BEFORE file removal (D2 phase 1a).
    if [[ "$stale" == *.plist ]]; then
      _stale_label="$(plist_label "$ROOT/$stale")" \
        || { _log "ERROR: cannot determine launchd Label from stale plist $ROOT/$stale -- refusing removal"; exit 1; }
      if [[ -n "${DOGANY_LAUNCHD_CAPTURE:-}" ]]; then
        printf 'launchctl bootout gui/UID/%s\n' "$_stale_label" >> "$DOGANY_LAUNCHD_CAPTURE"
        _log "  upgrade: (rehearsal) bootout captured for $_stale_label"
      else
        launchctl bootout "gui/$(id -u)/$_stale_label" >/dev/null 2>&1 || true
        rm -f "$LAUNCH_AGENTS_DIR/$(basename "$stale")" 2>/dev/null || true
        _log "  upgrade: booted out + unstaged launchd unit $_stale_label"
      fi
    fi
    # (b) NM3 backup, then remove.
    mkdir -p "$UPG_BACKUP_DIR/$(dirname "$stale")"
    cp -p "$ROOT/$stale" "$UPG_BACKUP_DIR/$stale"
    rm -f "$ROOT/$stale"
    _log "  upgrade: removed stale pack file: $stale (backup: files/_archive/$(basename "$UPG_BACKUP_DIR")/)"
  done < <(_ledger_paths "$OLD_LEDGER_SNAP")
  rm -f "$OLD_LEDGER_SNAP"
fi

# ---------- deps-provision: payload requirements.txt (DGN-850) ---------------
# Same kit<->pack dependency seam as the kit path (symmetric position: right
# before the DOGANY_PACKS upsert). Payload root for agent/module packs is
# <package_dir>/<reference_slug>. Zero-delta when no requirements.txt; a
# provisioning failure never fails the install (provisioner exits 0 on
# degrade by contract).
_DEPS_SH="$SCRIPT_DIR/pack_deps_provision.sh"
if [[ -f "$_DEPS_SH" ]]; then
  _log "step deps-provision (DGN-850)"
  _deps_out="$(bash "$_DEPS_SH" --root "$ROOT" --requirements "$PKG_PAYLOAD/requirements.txt" 2>&1)" || true
  while IFS= read -r _dl; do _log "  $_dl"; done <<< "$_deps_out"
else
  _log "deps-provision WARN -- pack_deps_provision.sh not found at $_DEPS_SH (skipped; consumers fall back)"
fi

# DGN-1456/1460: promote the evidence BEFORE the ledger projection (same
# order as the kit/pack path -- receipt first, DOGANY_PACKS second; a crash
# in between reads as MISMATCH, never as a completed install). Known limit,
# stated out loud: steps 7b..9 below still deliver AFTER this receipt on the
# legacy lane -- unchanged from the pre-existing upsert position, no new
# exposure.
_ev_rc=0
_ev_out="$(evidence_complete "$ROOT" "$PACK_ID")" || _ev_rc=$?
while IFS= read -r _ev_l; do [[ -n "$_ev_l" ]] && _log "  $_ev_l"; done <<< "$_ev_out"
if [[ "$_ev_rc" -ne 0 ]]; then
  _fail "install evidence: delivery-complete REFUSED -- ledger NOT updated, in-progress marker kept (DGN-1460 fail-closed)"
fi

# DGN-227 B3/P7: instance consumption record -- DOGANY_PACKS list-form upsert
# in .instance.conf (id@version entries; other packs' entries preserved).
# TK-13 U1: atomic primitive (flock + tmp/fsync/rename) -- same-wire with the
# kit path upsert above (lock L216 mandatory co-repair).
# RECORD_VERSION, not PACK_VERSION: the ledger records the version of the tag
# the payload came from (--source-tag, dsp t2pin); identical to PACK_VERSION
# when no tag was named.
_packs_upsert_atomic "$ROOT/.instance.conf" "$PACK_ID" "$RECORD_VERSION"
_log "  .instance.conf DOGANY_PACKS upserted ($PACK_ID@$RECORD_VERSION)"

# DGN-1031: requires_framework record for the framework-update-time compat
# gate (update.sh fw_reqframework_guard). Same-wire with the kit path record
# above.
_reqfw_record "$ROOT" "$PACK_ID" "$MANIFEST"

# DGN-1632: post-mint next-step slot record for the DGN-1487 selector
# (onboarding-check.py resolve_post_mint_next). Same-wire with the kit path
# record above; a write failure must not read as a successful install.
_post_mint_next_record "$ROOT" "$PACK_ID" "$MANIFEST" \
  || _fail "post_mint_next record write failed for $PACK_ID -- install NOT complete"

# DGN-1632 W3: pack-owned Q4 tone candidates record. Same-wire with the kit
# path record above.
_tone_candidates_record "$ROOT" "$PACK_ID" "$MANIFEST" \
  || _fail "tone_candidates record write failed for $PACK_ID -- install NOT complete"

# DGN-227 D1/P18 (replaces DGN-402 grill r2 MAJOR-2 predicate): reconcile
# pack-tagged preserve entries against the INSTALL LEDGER on every
# install/reinstall/upgrade; hand-written (untagged) entries never touched.
_preserve_reconcile

# ---------- STEP 7b: AGENT.md fragment append (rendered, idempotent) --------
# DGN-1467: marker-pair mechanics live in _persona_fragment_apply (shared
# with the contract-class path above); this call site keeps its position,
# order and argument meaning (DGN-1018 §5-B no-regression).
if has_cat agent_md_fragment; then
  _log "step 7b: AGENT.md fragment append"

  # DGN-773 R5/T4b: resolve to PROFILE.md (2.0, real file) or AGENT.md
  # (pre-2.0, real file) -- NEVER the AGENT.md symlink path itself, so the
  # excise-and-rename inside _persona_fragment_apply renames onto the real
  # target and never touches/clobbers the AGENT.md compat symlink.
  AGENT_MD="$(resolve_persona_md "$ROOT")"
  AGENT_ADD="$PKG_PAYLOAD/AGENT.md.add"

  if [[ -f "$AGENT_ADD" ]]; then
    if [[ ! -f "$AGENT_MD" ]]; then
      _fail "step 7b: AGENT.md not found at $AGENT_MD (mint incomplete?)"
    fi
    # Render the fragment through the same slug/root substitution as the
    # plists so slug-derived prose lands correctly (DGN-366 L2 step 7b).
    RENDERED_ADD="$(mktemp)"
    _render_to "$AGENT_ADD" "$RENDERED_ADD"
    _persona_fragment_apply "step 7b" "$AGENT_MD" "$RENDERED_ADD" "$PACK_ID" "$AGENT_MARKER" legacy \
      "  appended AGENT.md.add (rendered, marker pair) to AGENT.md (marker: $AGENT_MARKER)"
    rm -f "$RENDERED_ADD"
  else
    _log "  no AGENT.md.add in package (skipping)"
  fi

  _log "step 7b: AGENT.md fragment done"
else
  _log "step 7b: AGENT.md fragment SKIPPED (category agent_md_fragment not declared)"
fi

# ---------- STEP 7c: knowledge wiring selftest (DGN-402, zero-model) ---------
# DGN-1467: the run/log/fail mechanics live in _knowledge_selftest_run
# (shared with the contract-class path above). Warehouse packs: gates G1-G4.
# Warehouse-less packs: inverse check. G5 (live probes) stays manual.
_log "step 7c: knowledge wiring selftest"
_knowledge_selftest_run "step 7c"
_log "step 7c: knowledge wiring selftest done"

# ---------- STEP 8: domain seed (declaration-driven, DGN-284 #2) -------------
# Only when the manifest declares domain_seed. Decision 11: migration path =
# pending_data (digest job flips it to ready); fresh/standalone mint = ready.
if [[ "$DOMAIN_SEED_DECL" -eq 1 ]]; then
  DB="$ROOT/database/lifekit.db"
  if [[ "$MIGRATION" -eq 1 ]]; then
    CONSULT_SEED="pending_data"
  else
    CONSULT_SEED="ready"
  fi
  _log "step 8: consult_state seed ($CONSULT_SEED)"

  "$PACK_PY3" - "$DB" "$CONSULT_SEED" <<'PYEOF'
import sqlite3, sys

db = sys.argv[1]
seed = sys.argv[2]
conn = sqlite3.connect(db)

# Only seed if the config table exists and consult_state is absent
has_config = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='config'"
).fetchone()
if not has_config:
    print("[consult_state] config table absent -- ledger apply step may have missed; skipping seed")
    conn.close()
    sys.exit(0)

existing = conn.execute(
    "SELECT value FROM config WHERE key='consult_state'"
).fetchone()
if existing:
    print("[consult_state] already set to %r (idempotent)" % existing[0])
else:
    conn.execute(
        "INSERT INTO config (key, value) VALUES ('consult_state', ?)", (seed,)
    )
    conn.commit()
    print("[consult_state] seeded %s" % seed)
conn.close()
PYEOF
  _log "step 8: consult_state seed done"
else
  _log "step 8: domain seed SKIPPED (manifest declares no domain_seed)"
fi

# ---------- STEP 9: model config verify (requested model) -------------------
_log "step 9: settings.json model verify (requested: $MODEL_OPT)"

"$PACK_PY3" - "$SETTINGS" "$MODEL_OPT" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
requested = sys.argv[2]

with open(settings_path) as f:
    s = json.load(f)

model = s.get("model", "")
if model == requested:
    print("[model] confirmed %s (OK)" % requested)
elif not model:
    s["model"] = requested
    with open(settings_path, "w") as f:
        json.dump(s, f, indent=2)
        f.write("\n")
    print("[model] set to %s (was absent)" % requested)
else:
    # Pre-existing value differs from requested -- leave it alone (intentional).
    print("[model] already set to %r -- leaving as-is (pre-existing, intentional)" % model)
PYEOF
_log "step 9: model verify done"

# ---------- STEP 11: minting_state record (instance-dependent) ---------------
if [[ "$NO_STATE" -eq 1 ]]; then
  _log "step 11: minting_state record SKIPPED (--no-state)"
elif [[ -z "$INSTANCE_ROOT" ]]; then
  _log "step 11: minting_state record SKIPPED (--instance-root not supplied; instance-dependent step -- explicit skip, DGN-366 L1)"
else
  _log "step 11: record mint state (instance-root=$INSTANCE_ROOT)"
  # DGN-1513 w4: activation-link path, not the old skills-bundle path -- the
  # module now lands at skills/dogany-mint-agent/ (root-relocation), reachable via
  # the crew leader's .claude/skills/dogany-mint-agent link (bundle before the
  # switch, consume checkout after -- both valid). The old skills-bundle
  # path dies with the bundle directory, and even when found, minting_state.py
  # since DGN-1505 requires --path <dir> for accept -- without it this whole
  # step silently no-ops (below, "not found -- skipping") or the accept call
  # itself errors out, and official-pack mint completion stops being
  # recorded with NO loud signal (grill DGN-1513 F-A).
  STATE_PY="$INSTANCE_ROOT/.claude/skills/dogany-mint-agent/scripts/minting_state.py"
  if [[ -f "$STATE_PY" ]]; then
    # DGN-1513 F-6: same disease DGN-1008 fixed at step 6 (snapshot) -- the
    # `| while read` log pump plus `|| true` swallowed the accept call's exit
    # code, so a runtime failure (bad --path, state-file write error)
    # reported "done" regardless. Capture the status and surface it loudly.
    # Deliberately NOT a hard _fail: the install itself succeeded; what must
    # not happen is a missing accept record reading as a recorded one.
    # NOTE the capture form differs from step 6's bare PIPESTATUS read: under
    # this script's `set -euo pipefail`, an untested failing pipeline exits
    # the whole script BEFORE any capture line runs (measured), so the
    # pipeline must be status-tested (`|| rc=$?`; pipefail makes that status
    # the accept call's own, the log-pump while always exits 0).
    _ACCEPT_RC=0
    "$PACK_PY3" "$STATE_PY" accept "$DOMAIN_FIELD" --agent "$SLUG" --path "$ROOT" 2>&1 | while IFS= read -r line; do _log "  state: $line"; done || _ACCEPT_RC=$?
    if [[ "$_ACCEPT_RC" -ne 0 ]]; then
      _log "  WARN: minting_state accept exited $_ACCEPT_RC -- mint completion NOT recorded (see state: lines above)"
      _log "step 11: minting_state accept FAILED (rc=$_ACCEPT_RC)"
    else
      _log "step 11: minting_state accept done"
    fi
  else
    _log "  minting_state.py not found at $STATE_PY -- skipping accept record (explicit skip)"
  fi
fi

_log "=== pack-install DONE pack=$PACK_ID slug=$SLUG ==="
echo "[pack_install] DONE -- see $LOG_FILE"
