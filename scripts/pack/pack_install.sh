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
#                   [--instance-root <path>] [--catalog <file>]
#                   [--model <sonnet|opus|haiku>]
#                   [--migrate-from <peer-root>] [--no-start] [--no-state]
#                   [--dry-run]
#
# Arguments:
#   <slug>          agent slug (kebab-case, must match the minted instance)
#   <root>          absolute path to the minted instance root
#   --pack <id>     pack id from the catalog (required)
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
#   --no-start      skip the launchd bot-start step (safe for testing)
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
#   1. preflight checks (declared categories only)
#   2. package copy (declared categories: lib/ routines/ prompts/ plists/
#      db_migrations/ scripts/ triggers); plists are RENDERED, not copied:
#      launchd labels, plist filenames and reference paths are derived from
#      <slug>/<root> (DGN-284 #1). A bundled plists.defer is MERGE-APPENDED
#      into the instance framework defer (DGN-227 MINOR-5), not clobbered.
#   3. agent.conf fragment append (idempotent via manifest marker; peer
#      integration keys only on the migration path -- DGN-284 #3/#6)
#   4. W01 ledger apply CLI (only when the pack ships lib/ledger.py)
#   5. ledger-inject hook wiring (only when the pack ships
#      routines/ledger-inject.py)
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
#  10. bot start via mint_run.sh start (honoring plists.defer) [--no-start]
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
DEFAULT_CATALOG="$REPO_DIR/packs/catalog.json"

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

# DGN-1018 RR2: kit/service-namespace token grammar DATA (regex core +
# framework-reserved name list), shared with compat-lint.sh's independent
# duplicate validation -- see the lib file header for why this is data-only.
TOKEN_GRAMMAR_LIB="$SCRIPT_DIR/lib/pack_token_grammar.sh"
[[ -f "$TOKEN_GRAMMAR_LIB" && -r "$TOKEN_GRAMMAR_LIB" ]] \
  || { echo "ERROR: token grammar library missing ($TOKEN_GRAMMAR_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/pack_token_grammar.sh
. "$TOKEN_GRAMMAR_LIB"

# ---------- arg parse -------------------------------------------------------
SLUG="${1:-}"
ROOT="${2:-}"
PACK_ID="" DRY=0 NO_START=0 NO_STATE=0 MODEL_OPT="" MIGRATION=0 PEER_ROOT=""
INSTANCE_ROOT="" CATALOG="$DEFAULT_CATALOG" UPGRADE=0 PACK_DIR_OVERRIDE=""
shift 2 2>/dev/null || { echo "usage: pack_install.sh <slug> <root> --pack <id> [--pack-dir <dir>] [--instance-root <path>] [--catalog <file>] [--model <sonnet|opus|haiku>] [--migrate-from <peer-root>] [--upgrade] [--no-start] [--no-state] [--dry-run]" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack)          PACK_ID="$2"; shift 2 ;;
    --pack-dir)      PACK_DIR_OVERRIDE="$2"; shift 2 ;;
    --instance-root) INSTANCE_ROOT="$2"; shift 2 ;;
    --catalog)       CATALOG="$2"; shift 2 ;;
    --model)         MODEL_OPT="$2"; shift 2 ;;
    --migrate-from)  PEER_ROOT="$2"; MIGRATION=1; shift 2 ;;
    --upgrade)       UPGRADE=1; shift ;;
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

# ---------- NM3: payload checksum verification GATE (shared) -----------------
# _nm3_verify <package_dir>
#
# DGN-227 B4-5 / D2: before applying ANY payload, verify each shipped file
# against the pack's checksums.sha manifest (sha256). A mismatch or a listed
# file missing on disk = corrupt/tampered payload -> loud-FAIL the install
# (never warn-continue). checksums.sha lines are '<sha256hex>  <relpath>' with
# relpath relative to <package_dir> (the publish pipeline generates it, B4-5).
# Absent checksums.sha = legacy/pre-NM3 pack: loud WARN (no silent skip),
# install continues -- a published pack MUST ship it (publish gate, B4-5);
# this arms the gate for packs that carry it without breaking pre-NM3 packs.
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
  local pkg_dir="$1"
  local sums_file="$pkg_dir/checksums.sha"
  local nm3_out=""
  if [[ ! -f "$sums_file" ]]; then
    _log "NM3: WARN -- no checksums.sha in package ($sums_file) -- verification SKIPPED (legacy/dev pack; a published pack MUST ship it per B4-5, loud not silent)"
    return 0
  fi
  _log "NM3: verifying payload against checksums.sha"
  nm3_out="$(python3 - "$pkg_dir" "$sums_file" <<'PYEOF'
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
    _fail "NM3: payload checksum verification FAILED -- corrupt/tampered payload, install aborted (B4-5 gate)"
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
  python3 - "$conf" "$pid" "$ver" <<'PYEOF'
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
  val="$(python3 -c "
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
fi

# ---------- pack manifest (declaration-driven install) ------------------------
MANIFEST="$PACKAGE_DIR/pack-manifest.json"
[[ -f "$MANIFEST" ]] || _fail "pack-manifest.json not found: $MANIFEST (every pack must declare its categories -- no legacy fallback)"

_mf_field() { # _mf_field <key> -- string field ('' when absent)
  python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print(v if isinstance(v,str) else '')" "$MANIFEST" "$1"
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
_rk_verdict="$(python3 - "$MANIFEST" "$ROOT/.instance.conf" "$PACK_TOKEN_CORE" "${PACK_RESERVED_TOKENS[*]}" <<'PYEOF'
import json, re, sys, pathlib

def out(v):
    print(v)
    sys.exit(0)

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
    out("BLOCK:.instance.conf not found at %s -- cannot verify kit '%s' is "
        "installed" % (conf, kit))
try:
    conf_lines = conf.read_text(encoding="utf-8").splitlines()
except Exception as e:
    out("BLOCK:.instance.conf unreadable (%s) -- observed nothing, cannot "
        "proceed (DGN-1004)" % e)
packs_line = None
for ln in conf_lines:
    if ln.startswith("DOGANY_PACKS="):
        packs_line = ln
        break
if packs_line is None:
    out("BLOCK:no DOGANY_PACKS line in .instance.conf -- kit '%s' is not "
        "recorded as installed" % kit)
items = [x for x in packs_line.split("=", 1)[1].split(",") if x]
matches = [x for x in items if x.split("@", 1)[0] == kit]
if not matches:
    out("BLOCK:kit '%s' has no <kit>@ entry in DOGANY_PACKS -- required kit "
        "is not installed on this instance (install the kit first)" % kit)
entry = matches[0]
ver_str = entry.split("@", 1)[1] if "@" in entry else ""

def parse_semver(s):
    m = re.match(r'^(\d+)\.(\d+)\.(\d+)', s.strip())
    return tuple(int(x) for x in m.groups()) if m else None

ver = parse_semver(ver_str)
if ver is None:
    out("BLOCK:DOGANY_PACKS entry '%s' has an unparseable version -- cannot "
        "verify the range (unverifiable = no-go, DGN-1004)" % entry)
# Range evaluation: same operator semantics as compat-lint _semver_satisfies.
for t in tokens:
    m = re.match(r'^(>=|>|<=|<|==|!=)(.+)$', t)
    op, cv = m.group(1), parse_semver(m.group(2))
    if cv is None:
        out("BLOCK:requires_kit.range constraint version unparseable: %r" % t)
    if ((op == ">=" and not ver >= cv) or (op == ">" and not ver > cv)
            or (op == "<=" and not ver <= cv) or (op == "<" and not ver < cv)
            or (op == "==" and not ver == cv) or (op == "!=" and not ver != cv)):
        out("BLOCK:installed %s does not satisfy required range '%s'"
            % (entry, rng))
out("PASS:%s (range '%s' satisfied)" % (entry, rng))
PYEOF
)" || _rk_verdict="BLOCK:requires_kit gate crashed -- fail closed"
case "$_rk_verdict" in
  SKIP:*) _log "requires_kit gate: SKIP -- ${_rk_verdict#SKIP:}" ;;
  PASS:*) _log "requires_kit gate: PASS -- ${_rk_verdict#PASS:}" ;;
  BLOCK:*) _fail "requires_kit gate: ${_rk_verdict#BLOCK:} (DGN-681 S4b-1 install-time satisfaction check; fail-closed, no warn lane)" ;;
  *) _fail "requires_kit gate: unrecognized verdict '$_rk_verdict' -- fail closed" ;;
esac

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
  python3 -c "
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
  python3 -c "
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

# _kit_conf_merge <instance_conf> <fragment_conf>
# Merge-key: append keys from fragment_conf that are NOT already present in
# instance_conf. Existing keys are never modified (preserves hand-edited values).
# Both files use KEY=VALUE format (lines starting with # are comments, skipped).
_kit_conf_merge() {
  local inst="$1" frag="$2"
  python3 - "$inst" "$frag" <<'PYEOF'
import sys, re, pathlib

inst_path = pathlib.Path(sys.argv[1])
frag_path = pathlib.Path(sys.argv[2])

# Parse existing keys from instance config (KEY=... -> set of keys).
existing_keys = set()
inst_lines = inst_path.read_text(encoding="utf-8").splitlines() if inst_path.exists() else []
for line in inst_lines:
    line = line.rstrip("\r")
    if line.startswith("#") or not line.strip():
        continue
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
    if m:
        existing_keys.add(m.group(1))

# Append fragment keys not already present (merge-key: add-only, no clobber).
added = []
frag_text = frag_path.read_text(encoding="utf-8") if frag_path.exists() else ""
for line in frag_text.splitlines():
    line_stripped = line.rstrip("\r")
    if line_stripped.startswith("#") or not line_stripped.strip():
        continue
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=', line_stripped)
    if m:
        key = m.group(1)
        if key not in existing_keys:
            added.append(line_stripped)
            existing_keys.add(key)

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
PYEOF
}

# _kit_i18n_json_merge <inst_json> <bundle_json>
# Add-only merge of bundle JSON keys into the instance i18n JSON file.
# Existing keys in the instance are NEVER overwritten (idempotent, merge-key).
# Both files must be JSON objects ({key: string, ...}).
# Creates the instance file (and parent dir) if absent.
_kit_i18n_json_merge() {
  local inst="$1" bundle="$2"
  python3 - "$inst" "$bundle" <<'PYEOF'
import json, sys, pathlib

inst_path = pathlib.Path(sys.argv[1])
bundle_path = pathlib.Path(sys.argv[2])

# Load bundle (source of truth for new keys).
with open(bundle_path, encoding="utf-8") as f:
    bundle = json.load(f)

# Load existing instance JSON (or empty dict if absent).
if inst_path.exists():
    with open(inst_path, encoding="utf-8") as f:
        inst = json.load(f)
else:
    inst = {}

# Add-only: copy keys from bundle that are not already in inst.
added = []
for k, v in bundle.items():
    if k not in inst:
        inst[k] = v
        added.append(k)

# Write back (only if there are new keys to add).
if added:
    inst_path.parent.mkdir(parents=True, exist_ok=True)
    with open(inst_path, "w", encoding="utf-8") as f:
        json.dump(inst, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("[kit-i18n-merge] added %d keys: %s" % (len(added), ", ".join(added[:5]) + (" ..." if len(added) > 5 else "")))
else:
    print("[kit-i18n-merge] no new keys to add (all already present)")
PYEOF
}

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
KIT_SKILL_MODE_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
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
  python3 -c "
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

# ---------- pack class dispatch (DGN-1018 §5-B) ------------------------------
# Contract class axis: kind=kit OR (kind=pack AND contract_version present)
# takes the payload_root pipeline below. A legacy dev pack (kind absent AND
# contract_version absent, measured) satisfies NEITHER condition and keeps the
# agent/module path below byte-identical -- no-regression hard requirement.
# Contract presence uses the same "genuinely observed" rule as the
# requires_kit gate above (None or "" == absent).
PACK_CONTRACT_PRESENT="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get('contract_version'); print(0 if (v is None or v=='') else 1)" "$MANIFEST" 2>/dev/null || echo 0)"
PACK_CLASS=""
if [[ "$PACK_KIND" == "kit" ]]; then
  PACK_CLASS="kit"
elif [[ "$PACK_KIND" == "pack" && "$PACK_CONTRACT_PRESENT" -eq 1 ]]; then
  PACK_CLASS="pack"
fi

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
  _kind_verdict="$(python3 -c "
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
  mkdir -p "$LOG_DIR"

  if [[ "$PACK_CLASS" == "kit" ]]; then
    _log "=== kit-install START pack=$PACK_ID provides_kit=$KIT_PROVIDES slug=$SLUG root=$ROOT ==="
  else
    _log "=== pack-install START (behavior-pack profile, DGN-1018) pack=$PACK_ID slug=$SLUG root=$ROOT ==="
  fi

  # ---- preflight ----
  KIT_PAYLOAD_DIR="$PACKAGE_DIR/$KIT_PAYLOAD_ROOT"
  [[ -d "$KIT_PAYLOAD_DIR" ]] || _fail "kit payload root not found: $KIT_PAYLOAD_DIR"
  [[ -d "$ROOT" ]] || _fail "instance root not found: $ROOT"

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
  SERVICE_NS=""
  _pack_inline_verdict="$(python3 - "$MANIFEST" "$PACK_TOKEN_CORE" "${PACK_RESERVED_TOKENS[*]}" <<'PYEOF'
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

# capabilities.db_lane for kind=pack: absent => false; true => refused
# (kit-exclusive surface); malformed => refused (compat-lint C1 twin).
caps = d.get("capabilities")
if caps is not None:
    if not isinstance(caps, dict):
        out("BLOCK:capabilities is not an object")
    unknown = sorted(set(caps) - {"db_lane"})
    if unknown:
        out("BLOCK:unknown capabilities key(s): %s"
            % ", ".join(repr(k) for k in unknown))
    v = caps.get("db_lane")
    if v is not None and not isinstance(v, bool):
        out("BLOCK:capabilities.db_lane must be a JSON boolean, got %r" % (v,))
    if v is True:
        out("BLOCK:kind=pack declares capabilities.db_lane=true -- the DB "
            "lane is a kit-exclusive surface")

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
  _log "pack: behavior-pack profile: no kit identity; service_namespace='${SERVICE_NS:-}' db_lane=0 (DGN-1018 §5-B)"
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
  _pk_present="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if 'provides_kit' in d else 0)" "$MANIFEST" 2>/dev/null || echo 0)"
  if [[ "$_pk_present" -eq 0 ]]; then
    _fail "kit: manifest missing provides_kit -- the legacy 'lifekit' default was removed (DGN-1143: zero published manifests relied on it, and after the dec-145 kit rename it would silently resolve to a nonexistent kit). Declare provides_kit explicitly. (lockstep with compat-lint C1)"
  fi
  if [[ "$_pk_present" -eq 1 ]]; then
    # Same extraction semantics as compat-lint _mf_str (str/int/float pass
    # through as text, every other JSON type collapses to '' and fails the
    # regex below -- all-type fail-closed, DGN-1002 review note).
    KIT_NAME="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=d.get('provides_kit'); print(v if isinstance(v,(str,int,float)) else '')" "$MANIFEST" 2>/dev/null || echo "")"
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
  _caps_verdict="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
caps = d.get("capabilities")
if caps is None:
    print("absent"); sys.exit(0)
if not isinstance(caps, dict):
    print("invalid: capabilities is not an object"); sys.exit(0)
unknown = sorted(set(caps) - {"db_lane"})
if unknown:
    print("invalid: unknown capabilities key(s): %s" % ", ".join(repr(k) for k in unknown)); sys.exit(0)
v = caps.get("db_lane")
if v is None:
    print("absent"); sys.exit(0)   # empty object == nothing declared
if not isinstance(v, bool):
    print("invalid: capabilities.db_lane must be a JSON boolean, got %r" % (v,)); sys.exit(0)
print("true" if v else "false")
PYEOF
)" || _caps_verdict="invalid: manifest JSON unreadable"
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
  # service_namespace on kind=kit = dual truth-source, refused (DGN-1018 §4;
  # a kit's service namespace canonical source is provides_kit). Inline twin
  # of the compat-lint C1 rule -- the lint gate below may be absent.
  _ns_on_kit="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if 'service_namespace' in d else 0)" "$MANIFEST" 2>/dev/null || echo 0)"
  if [[ "$_ns_on_kit" -eq 1 ]]; then
    _fail "kit: manifest declares service_namespace -- a kit's service namespace canonical source is provides_kit (dual truth-source refused; verdict-identical with compat-lint C1, DGN-1018)"
  fi

  _log "kit: kit-name: $KIT_NAME (provides_kit declared) db_lane=$KIT_DB_LANE"
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
  if [[ "$PACK_CLASS" == "kit" ]]; then
    SHARED_ROOT="$SHARED_HOME/crews/$KIT_NAME/shared-skills"
  else
    SHARED_ROOT=""
  fi

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
  # Verdict: any DESTRUCTIVE file => BLOCK (_fail, nothing written). Bypass
  #   requires DOGANY_PACK_ACCEPT_CONTENT_LOSS=<pack id> (exact id match);
  #   the accepted loss is still fully enumerated in the log as the receipt.
  # Scanned surfaces mirror the copy steps 1:1: kit_core database/ (skipped
  #   when KIT_CORE_SKIP=1 -- nothing will be copied there), kit_mirror
  #   mirror/ (same excludes, and only when the mirror pin guard below will
  #   let the copy proceed -- decision replicated in lockstep), own-mode
  #   skills-bundle, routines/bundle, service facade. share-mode skills land
  #   in the crew-shared root, not this instance -- NOT scanned (limitation).
  _CD_ARGS=()
  _CD_TAB="$(printf '\t')"
  if [[ "$PACK_CLASS" == "kit" && "$KIT_CORE_SKIP" -eq 0 && -d "$KIT_PAYLOAD_DIR/database" ]]; then
    _CD_ARGS[${#_CD_ARGS[@]}]="database${_CD_TAB}$KIT_PAYLOAD_DIR/database${_CD_TAB}$ROOT/database"
  fi
  if [[ "$PACK_CLASS" == "kit" && -d "$KIT_PAYLOAD_DIR/mirror" ]]; then
    # Lockstep replica of the kit_mirror guard decision below: scan mirror/
    # only in the branches where KIT_MIRROR_SKIP stays 0 (copy will happen).
    _cd_mirror_scan=0
    _cd_p_sb="$KIT_PAYLOAD_DIR/mirror/sdk_bridge.py"
    _cd_i_sb="$ROOT/mirror/sdk_bridge.py"
    if [[ -f "$_cd_p_sb" && -f "$_cd_i_sb" ]]; then
      _cd_pp="$(_kit_extract_ver_sdk_bridge_py "$_cd_p_sb" 2>/dev/null || true)"
      _cd_ip="$(_kit_extract_ver_sdk_bridge_py "$_cd_i_sb" 2>/dev/null || true)"
      if [[ "$_cd_pp" =~ ^[0-9]+$ && "$_cd_ip" =~ ^[0-9]+$ && "$_cd_ip" -le "$_cd_pp" ]]; then
        _cd_mirror_scan=1
      fi
    elif [[ -f "$_cd_p_sb" && ! -f "$_cd_i_sb" ]]; then
      _cd_mirror_scan=1
    elif [[ ! -f "$_cd_p_sb" && ! -f "$_cd_i_sb" ]]; then
      _cd_mirror_scan=1
    fi
    if [[ "$_cd_mirror_scan" -eq 1 ]]; then
      _CD_ARGS[${#_CD_ARGS[@]}]="mirror${_CD_TAB}$KIT_PAYLOAD_DIR/mirror${_CD_TAB}$ROOT/mirror"
    fi
  fi
  if [[ -d "$KIT_PAYLOAD_DIR/skills-bundle" ]]; then
    while IFS= read -r _cd_sd; do
      [[ -d "$_cd_sd" ]] || continue
      _cd_sn="$(basename "$_cd_sd")"
      if [[ "$(_kit_skill_mode "$_cd_sn")" == "share" ]]; then continue; fi
      _CD_ARGS[${#_CD_ARGS[@]}]="skills-bundle/$_cd_sn${_CD_TAB}$_cd_sd${_CD_TAB}$ROOT/.claude/skills-bundle/$_cd_sn"
    done < <(find "$KIT_PAYLOAD_DIR/skills-bundle" -mindepth 1 -maxdepth 1 -type d | sort)
  fi
  if [[ -d "$KIT_PAYLOAD_DIR/routines/bundle" ]]; then
    _CD_ARGS[${#_CD_ARGS[@]}]="routines/bundle${_CD_TAB}$KIT_PAYLOAD_DIR/routines/bundle${_CD_TAB}$ROOT/routines/bundle"
  fi
  _cd_svc_ns="$KIT_NAME"
  if [[ "$PACK_CLASS" == "pack" ]]; then _cd_svc_ns="$SERVICE_NS"; fi
  if [[ -n "$_cd_svc_ns" && -d "$KIT_PAYLOAD_DIR/service/$_cd_svc_ns" ]]; then
    _CD_ARGS[${#_CD_ARGS[@]}]="service/$_cd_svc_ns${_CD_TAB}$KIT_PAYLOAD_DIR/service/$_cd_svc_ns${_CD_TAB}$ROOT/service/$_cd_svc_ns"
  fi
  # Scanner logic is SINGLE-SOURCED in scripts/pack/lib/content_drift.py
  # (DGN-1118 공용화): the same judgement also guards the update.sh lane, so
  # a second embedded copy here would be the DGN-1034 shape (two gates, one
  # contract). This lane passes dir specs only, no baseline / subst / filter
  # flags -- the lib's behavior for that input is byte-identical to the
  # heredoc it was extracted from.
  _CD_LIB="${DOGANY_CONTENT_DRIFT_LIB:-$SCRIPT_DIR/lib/content_drift.py}"
  [[ -f "$_CD_LIB" ]] || _fail "$PACK_CLASS: CONTENT-DRIFT GUARD (DGN-1109): scanner lib missing ($_CD_LIB) -- torn tree, cannot prove the copy is safe, fail-closed (DGN-1004)"
  _cd_rc=0
  _cd_out="$(python3 "$_CD_LIB" ${_CD_ARGS[@]+"${_CD_ARGS[@]}"})" || _cd_rc=$?
  if [[ "$_cd_rc" -eq 4 ]]; then
    printf '%s\n' "============================================================"
    _log "$PACK_CLASS: CONTENT-DRIFT GUARD (DGN-1109): pins allow the copy, but the payload would ERASE live content:"
    while IFS= read -r _cd_l; do _log "  $_cd_l"; done <<< "$_cd_out"
    if [[ "${DOGANY_PACK_ACCEPT_CONTENT_LOSS:-}" == "$PACK_ID" ]]; then
      _log "$PACK_CLASS: CONTENT-DRIFT GUARD: loss ACCEPTED by operator (DOGANY_PACK_ACCEPT_CONTENT_LOSS=$PACK_ID) -- proceeding; the enumeration above is the receipt"
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

  if [[ "$DRY" -eq 1 ]]; then
    _log "dry-run: $PACK_CLASS install plan for $PACK_ID -> $ROOT"
    _log "dry-run: payload=$KIT_PAYLOAD_DIR"
    if [[ "$PACK_CLASS" == "kit" ]]; then
    _log "dry-run: kit_core_skip=$KIT_CORE_SKIP"
    _log "dry-run: provides_kit=$KIT_PROVIDES pack_version=$PACK_VERSION"
    _log "dry-run: kit_name=$KIT_NAME db_lane=$KIT_DB_LANE"
    else
    _log "dry-run: behavior-pack profile (DGN-1018): kit_core/db-init/migrate/mirror NOT executed"
    _log "dry-run: service_namespace='${SERVICE_NS:-}' pack_version=$PACK_VERSION"
    fi
    if [[ -f "$KIT_PAYLOAD_DIR/requirements.txt" ]]; then
      _log "dry-run: deps-provision: payload/requirements.txt present -> would pip-provision runtime interpreters (DGN-850)"
    else
      _log "dry-run: deps-provision: no payload/requirements.txt -> no-op"
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
      done < <(find "$KIT_PAYLOAD_DIR/skills-bundle" -mindepth 1 -maxdepth 1 -type d | sort)
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
  _nm3_verify "$PACKAGE_DIR"

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
        _dst="$ROOT/database/$_rel"
        mkdir -p "$(dirname "$_dst")"
        cp -f "$_src" "$_dst"
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
  _KIT_SCHEMA="$ROOT/database/schema.sql"
  _KIT_DB="$ROOT/database/$KIT_NAME.db"
  if [[ "$KIT_DB_LANE" -eq 0 ]]; then
    _log "kit: db-init: capabilities.db_lane=false -- SKIP (kit declares no DB lane)"
  elif [[ -f "$_KIT_DB" ]]; then
    _log "kit: db-init: $KIT_NAME.db exists -> keep (idempotent)"
  elif [[ ! -f "$_KIT_SCHEMA" ]]; then
    _log "kit: db-init: no schema.sql at instance database/ -- skipping (payload shipped none)"
  elif ! command -v sqlite3 >/dev/null 2>&1; then
    _fail "kit: db-init: sqlite3 not found -- cannot initialize $KIT_NAME.db (DGN-674 F7 hard fail)"
  else
    # Atomic init (DGN-783 B2): load the schema into a tmp DB, then mv into
    # place. A mid-load failure previously left a partial DB file that the
    # exists->keep branch above then froze permanently (with exit 0).
    _KIT_DB_TMP="${_KIT_DB}.init-tmp.$$"
    rm -f "$_KIT_DB_TMP"
    if sqlite3 "$_KIT_DB_TMP" < "$_KIT_SCHEMA"; then
      mv "$_KIT_DB_TMP" "$_KIT_DB"
      _log "kit: db-init: initialized $KIT_NAME.db from schema.sql (empty structured lane)"
    else
      rm -f "$_KIT_DB_TMP"
      _fail "kit: db-init: schema.sql failed to load -- partial DB discarded, $KIT_NAME.db NOT created"
    fi
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
      if [[ "$_cur_ver" -gt "$_target_pin" ]]; then
        _fail "kit: migrate: DB user_version=$_cur_ver is AHEAD of code pin=$_target_pin -- refusing (forward-only; reconcile the migration lineage first)"
      fi
      if [[ "$_cur_ver" -lt "$_target_pin" ]]; then
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
      _self_recorded="$(python3 - "$ROOT/.instance.conf" "$PACK_ID" <<'PYEOF'
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
        _fail "pack: service conflict BLOCK: $ROOT/service/$_SVC_NS already exists and pack id '$PACK_ID' has no DOGANY_PACKS entry (first install) -- refusing to overwrite another pack/kit's service surface (DGN-1018 §5-B); re-install/upgrade proceeds only when the own id is already recorded"
      fi
      _log "pack: service_facade: $ROOT/service/$_SVC_NS exists and own id '$PACK_ID' is recorded (re-install/upgrade) -- proceeding"
    fi
    _log "$PACK_CLASS: step service_facade: service/$_SVC_NS/ copy"
    mkdir -p "$ROOT/service/$_SVC_NS"
    while IFS= read -r _src; do
      [[ -e "$_src" ]] || continue
      [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
      _rel="${_src#"$KIT_PAYLOAD_DIR/service/$_SVC_NS/"}"
      _dst="$ROOT/service/$_SVC_NS/$_rel"
      mkdir -p "$(dirname "$_dst")"
      cp -f "$_src" "$_dst"
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
    KIT_MIRROR_SKIP=0
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
        _dst="$ROOT/mirror/$_rel"
        mkdir -p "$(dirname "$_dst")"
        cp -f "$_src" "$_dst"
        _log "  kit_mirror: $_rel"
      done < <(find "$KIT_PAYLOAD_DIR/mirror" -type f)
      # B1/B3 (DGN-855): drop sentinel so update.sh 3e-mirror knows this
      # instance's mirror/ is pack-owned.  Written ONLY on a successful delivery
      # (not when the reverse-drift guard SKIPs). Idempotent overwrite is fine.
      # Sentinel is existence-checked by update.sh 3e-mirror (content is a
      # human breadcrumb only); name the owning kit (DGN-1003).
      printf '# %s pack owns mirror/ (DGN-855)\n' "$KIT_NAME" > "$ROOT/mirror/.pack-owned"
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
        _share_n=$((_share_n + 1))
        continue
      fi
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
        _dst="$_dst_dir/$_rel"
        mkdir -p "$(dirname "$_dst")"
        cp -f "$_src" "$_dst"
        _log "  skills-bundle: $_skill_name/$_rel"
      done < <(find "$_skill_dir" -type f)
    done < <(find "$KIT_PAYLOAD_DIR/skills-bundle" -mindepth 1 -maxdepth 1 -type d | sort)
    if [[ "$_share_n" -gt 0 ]]; then
      _log "kit: skills-bundle: share summary: $_share_n skill(s) referenced from $SHARED_ROOT"
    fi
    _log "$PACK_CLASS: skills-bundle done"
  else
    _log "$PACK_CLASS: skills-bundle: no payload/skills-bundle/ -- skipping"
  fi

  # ---- routines/bundle: payload/routines/bundle/* -> instance/routines/bundle/ ----
  if [[ -d "$KIT_PAYLOAD_DIR/routines/bundle" ]]; then
    _log "$PACK_CLASS: step routines-bundle: routines/bundle/ install"
    mkdir -p "$ROOT/routines/bundle"
    while IFS= read -r _src; do
      [[ -e "$_src" ]] || continue
      [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
      _rel="${_src#"$KIT_PAYLOAD_DIR/routines/bundle/"}"
      _dst="$ROOT/routines/bundle/$_rel"
      mkdir -p "$(dirname "$_dst")"
      cp -f "$_src" "$_dst"
      [[ "$_dst" == *.sh ]] && chmod +x "$_dst"
      _log "  routines-bundle: $_rel"
    done < <(find "$KIT_PAYLOAD_DIR/routines/bundle" -type f)
    _log "$PACK_CLASS: routines-bundle done"
  else
    _log "$PACK_CLASS: routines-bundle: no payload/routines/bundle/ -- skipping"
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
      _log "  config-merge $_CONF_OWNER.conf: $_merge_out"
    fi
    # i18n fragment files at config/ top level (KEY=VALUE format)
    while IFS= read -r _src; do
      [[ -e "$_src" ]] || continue
      [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
      _fname="$(basename "$_src")"
      _merge_out="$(_kit_conf_merge "$ROOT/config/$_fname" "$_src")"
      _log "  config-merge $_fname: $_merge_out"
    done < <(find "$KIT_PAYLOAD_DIR/config" -maxdepth 1 -type f \( -name 'i18n.*' -o -name 'i18n_*' \))
    # i18n bundle JSON files in config/i18n/*.bundle.json (JSON object merge).
    # <lang>.bundle.json -> instance config/i18n/<lang>.json (add-only, idempotent).
    if [[ -d "$KIT_PAYLOAD_DIR/config/i18n" ]]; then
      _log "kit: step i18n-json-merge: config/i18n/*.bundle.json"
      while IFS= read -r _src; do
        [[ -f "$_src" ]] || continue
        [[ "$(basename "$_src")" == ".gitkeep" ]] && continue
        _bname="$(basename "$_src")"
        # Map <lang>.bundle.json -> <lang>.json
        _lang="${_bname%.bundle.json}"
        _inst_json="$ROOT/config/i18n/${_lang}.json"
        _merge_out="$(_kit_i18n_json_merge "$_inst_json" "$_src")"
        _log "  i18n-json-merge ${_lang}.json: $_merge_out"
      done < <(find "$KIT_PAYLOAD_DIR/config/i18n" -maxdepth 1 -type f -name '*.bundle.json')
      _log "kit: i18n-json-merge done"
    fi
    _log "$PACK_CLASS: config-merge done"
  else
    _log "$PACK_CLASS: config-merge: no payload/config/ -- skipping"
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

  # DGN-1018 §7 residual 1: knowledge/ is a SHAPE-ONLY surface for now --
  # compat-lint C4 allows it for kind=pack, but the delivery mechanism is
  # DEFERRED (bound to the unresolved DGN-807 D1-D6 design). Loud non-delivery
  # log so the deferral is never silent.
  if [[ "$PACK_CLASS" == "pack" && -d "$KIT_PAYLOAD_DIR/knowledge" ]]; then
    _log "pack: knowledge/: delivery DEFERRED (shape-only surface; delivery machinery bound to DGN-807 D1-D6 -- DGN-1018 §7 residual 1). Files NOT installed."
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
  python3 - "$ROOT/.instance.conf" "$MANIFEST" "$KIT_NAME" <<'PYEOF'
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

  # ---- DOGANY_PACKS upsert (existing machinery, DGN-227 B3/P7) ----
  # TK-13 U1: atomic primitive (flock + tmp/fsync/rename) -- same-wire with
  # the agent/module path upsert below (lock L216 mandatory co-repair).
  _packs_upsert_atomic "$ROOT/.instance.conf" "$PACK_ID" "$PACK_VERSION"
  _log "  .instance.conf DOGANY_PACKS upserted ($PACK_ID@$PACK_VERSION)"

  # DGN-1031: requires_framework record for the framework-update-time compat
  # gate (update.sh fw_reqframework_guard). Same-wire with the agent/module
  # path record below.
  _reqfw_record "$ROOT" "$PACK_ID" "$MANIFEST"

  if [[ "$PACK_CLASS" == "kit" ]]; then
    _log "=== kit-install DONE pack=$PACK_ID slug=$SLUG provides_kit=$KIT_NAME ==="
    echo "[pack_install] DONE (kit) -- see $LOG_FILE"
  else
    _log "=== pack-install DONE (behavior-pack profile) pack=$PACK_ID slug=$SLUG service_namespace='${SERVICE_NS:-}' ==="
    echo "[pack_install] DONE (pack) -- see $LOG_FILE"
  fi
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
DOMAIN_SEED_DECL="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if d.get('domain_seed') else 0)" "$MANIFEST")"

# Validate every declared category name against the known whitelist (M3).
# An unknown name is a manifest error -- FATAL at preflight with a clear message.
KNOWN_CATEGORIES="lib routines plists prompts agent_conf_fragment triggers db_migrations skills agent_md_fragment scripts knowledge_snapshot"

_cat_validate_out="$(python3 - "$MANIFEST" "$KNOWN_CATEGORIES" <<'PYEOF'
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
CATEGORY_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
for c in d.get("categories", []):
    print("%s\t%d" % (c["category"], 1 if c.get("required") else 0))
PYEOF
)"

# optional explicit file list for the lib category (backward-compat: the
# health pack lib/ carries peer-side files that must NOT deploy)
LIB_FILES="$(python3 - "$MANIFEST" <<'PYEOF'
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
KNOWLEDGE_DECL="$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(1 if isinstance(d.get('knowledge'), dict) else 0)" "$MANIFEST")"

KNOW_WAREHOUSE="" KNOW_SOURCE="" KNOW_SMOKE_ITEM="" KNOW_SMOKE_ARGS=""
KNOW_CONSUMER_LINES="" KNOW_TURN_LINES="" KNOW_CONSUMER_IDS=""
if [[ "$KNOWLEDGE_DECL" -eq 1 ]]; then
  _know_str() { # _know_str <key> -- knowledge.<key> string field ('' when absent)
    python3 -c "import json,sys; k=json.load(open(sys.argv[1])).get('knowledge') or {}; v=k.get(sys.argv[2]); print(v if isinstance(v,str) else '')" "$MANIFEST" "$1"
  }
  KNOW_WAREHOUSE="$(_know_str warehouse)"
  KNOW_SOURCE="$(_know_str source)"
  KNOW_SMOKE_ITEM="$(_know_str smoke_item)"
  KNOW_SMOKE_ARGS="$(_know_str smoke_args)"
  # '~' expansion for the publisher source path (spec S1 layer 1)
  KNOW_SOURCE="${KNOW_SOURCE/#\~/$HOME}"
  # consumer_skills as "skill<TAB>domain domain ..." lines
  KNOW_CONSUMER_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
import json, sys
k = json.load(open(sys.argv[1])).get("knowledge") or {}
for skill, domains in (k.get("consumer_skills") or {}).items():
    if not isinstance(domains, list):
        domains = []
    print("%s\t%s" % (skill, " ".join(str(d) for d in domains)))
PYEOF
)"
  # turns as "type<TAB>home" lines
  KNOW_TURN_LINES="$(python3 - "$MANIFEST" <<'PYEOF'
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
NET_NEW_SKILLS="$(python3 - "$MANIFEST" <<'PYEOF'
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
  sed "${sed_args[@]}" "$1" > "$2"
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
_subst_mint_tokens() {
  local f="$1" tmp
  local agent_name="" agent_label="" user_label="" agent_prefix="" agent_lang=""
  if [[ -f "$ROOT/.instance.conf" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "$ROOT/.instance.conf"
    agent_name="${DOGANY_AGENT_NAME:-}"
    agent_label="${DOGANY_AGENT_LABEL:-}"
    user_label="${DOGANY_USER_LABEL:-}"
    # optional field (absent on pre-DGN-213 instances) -- same fallback as update.sh
    agent_prefix="${DOGANY_AGENT_PREFIX:-[agent]}"
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
    _log "  WARN: instance identity incomplete (.instance.conf) -- identity token substitution skipped (any residue FAILs the knowledge selftest)"
  fi
  tmp="$(mktemp)"
  sed "${sed_args[@]}" "$f" > "$tmp"
  mv "$tmp" "$f"
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

# ---------------------------------------------------------------------------
# DGN-227 B3/P25: installed-files ledger (config/packs/<id>.files).
# Single record function called at EVERY copy point (H1-9: the ledger is not
# a side product -- every install write funnels through _ledger_record).
# The ledger is (a) the --upgrade removal-diff source (D2) and (b) the
# preserve-reconcile verdict source (D1).
# ---------------------------------------------------------------------------
LEDGER_DIR="$ROOT/config/packs"
LEDGER_FILE="$LEDGER_DIR/$PACK_ID.files"
LEDGER_STAGE=""

_ledger_record() { # _ledger_record <root-relative-path>
  [[ -n "$LEDGER_STAGE" ]] || LEDGER_STAGE="$(mktemp)"
  printf '%s\n' "$1" >> "$LEDGER_STAGE"
}

_ledger_finalize() {
  mkdir -p "$LEDGER_DIR"
  {
    echo "# pack install ledger -- DGN-227 B3/P25"
    echo "# pack: $PACK_ID"
    echo "# pack_version: $PACK_VERSION"
    echo "# installed: $(date '+%Y-%m-%dT%H:%M:%S')"
    if [[ -n "$LEDGER_STAGE" && -s "$LEDGER_STAGE" ]]; then
      sort -u "$LEDGER_STAGE"
    fi
  } > "$LEDGER_FILE"
  [[ -n "$LEDGER_STAGE" ]] && rm -f "$LEDGER_STAGE"
  _log "  ledger written: config/packs/$PACK_ID.files"
}

# _ledger_paths <file> -- entries only (comments stripped).
_ledger_paths() {
  [[ -f "$1" ]] || return 0
  grep -v '^#' "$1" | grep -v '^[[:space:]]*$' || true
}

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
command -v python3 >/dev/null 2>&1 || { _log "PREFLIGHT FAIL: python3 not found"; fail=1; }

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
  _log "dry-run: no-start=$NO_START"
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
# payload write, exactly as it always has on this path.
_nm3_verify "$PACKAGE_DIR"

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
  _log "compat-lint: WARN -- compat-lint.sh not found at $_COMPAT_LINT -- gate SKIPPED (install DGN-803 LS-3 not deployed)"
fi

# DGN-227 D2 note: the --upgrade stale-removal phase (ledger diff + bootout +
# NM3 backup + removal + ledger re-record) lives AFTER the apply steps, next
# to _preserve_reconcile -- the old ledger stays untouched on disk until
# _ledger_finalize there, so the diff source survives the whole apply pass.
if [[ "${UPGRADE:-0}" -eq 1 ]]; then
  _log "=== pack-install UPGRADE pack=$PACK_ID slug=$SLUG root=$ROOT ==="
fi

# ---------- STEP 2: package copy (declared categories only) ------------------
_log "step 2: package copy"

# 2a. lib/ -> routines/lib/
if has_cat lib && [[ -d "$PKG_LIB" ]]; then
  mkdir -p "$ROOT/routines/lib"
  if [[ -n "$LIB_FILES" ]]; then
    while IFS= read -r f; do
      [[ -n "$f" ]] || continue
      if [[ -f "$PKG_LIB/$f" ]]; then
        cp -f "$PKG_LIB/$f" "$ROOT/routines/lib/$f"
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
      cp -f "$f" "$ROOT/routines/lib/$(basename "$f")"
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
    cp -f "$f" "$ROOT/routines/"
    chmod +x "$ROOT/routines/$(basename "$f")"
    _ledger_record "routines/$(basename "$f")"
    _preserve_register "routines/$(basename "$f")"
    _log "  copied routines/$(basename "$f")"
  done
  for f in "$PKG_PAYLOAD/routines/"*.py; do
    [[ -e "$f" ]] || continue
    cp -f "$f" "$ROOT/routines/"
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
      cp "$_pack_defer" "$_inst_defer"
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
  cp -rf "$PKG_PAYLOAD/routines/prompts/." "$ROOT/routines/prompts/"
  while IFS= read -r _pf; do
    _rel="routines/prompts/${_pf#"$PKG_PAYLOAD/routines/prompts/"}"
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
    cp -f "$f" "$ROOT/database/migrations/"
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
    cp -f "$f" "$ROOT/scripts/"
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
  cp -f "$PKG_PAYLOAD/config/triggers.yaml" "$ROOT/config/triggers.yaml"
  _ledger_record "config/triggers.yaml"
  _log "  copied config/triggers.yaml"
else
  has_cat triggers || _log "  category triggers not declared -- skipping"
fi

_log "step 2: package copy done"

# ---------- STEP 3: agent.conf fragment append (idempotent) -----------------
if has_cat agent_conf_fragment; then
  _log "step 3: agent.conf append"

  CONF_ADD="$PKG_PAYLOAD/config/agent.conf.add"
  AGENT_CONF="$ROOT/config/agent.conf"
  MARKER="$CONF_MARKER"

  # Peer-integration keys belong to the MIGRATION path only (DGN-284 #3/#6).
  # DGN-227 E2-1/P24: MIGRATION_PEER joins the migration key family (fresh
  # strips it). HANDOFF_PEER_MAIN is a BRIEFING-topology key, NOT a migration
  # key -- it is deliberately NOT in this strip list (fresh paths may write it).
  PEER_KEYS_RE='^(L1_DB|L1_EXPECTED_USER_VERSION|HANDOFF_PEER_AG|MIGRATION_PEER)='

  # DGN-227 D2/P8: fragments are managed as BEGIN/END marker-pair blocks so
  # --upgrade can replace them (remove-and-reappend). Legacy single-marker
  # blocks cannot be bounded mechanically -> --upgrade loud-FAILs on them.
  CONF_PAIR_BEGIN="# DOGANY-PACK:$PACK_ID:BEGIN"
  CONF_PAIR_END="# DOGANY-PACK:$PACK_ID:END"

  if [[ -f "$CONF_ADD" ]]; then
    if [[ "$UPGRADE" -eq 1 ]] && grep -qF "$CONF_PAIR_BEGIN" "$AGENT_CONF" 2>/dev/null; then
      # marker-pair replacement: excise the old block, then fall through to append
      _tmp_conf="$(mktemp)"
      awk -v b="$CONF_PAIR_BEGIN" -v e="$CONF_PAIR_END" '
        $0 == b { inblk=1; next }
        $0 == e { inblk=0; next }
        !inblk { print }' "$AGENT_CONF" > "$_tmp_conf"
      mv "$_tmp_conf" "$AGENT_CONF"
      _log "  upgrade: excised prior agent.conf fragment block (marker pair)"
    elif [[ "$UPGRADE" -eq 1 ]] && grep -qF "$MARKER" "$AGENT_CONF" 2>/dev/null; then
      _fail "step 3: --upgrade found a LEGACY single-marker fragment block (no BEGIN/END pair) in agent.conf -- cannot bound it mechanically. Manual migration: remove the old block, then re-run (loud-FAIL by design, DGN-227 D2)"
    fi

    if grep -qF "$CONF_PAIR_BEGIN" "$AGENT_CONF" 2>/dev/null \
       || { [[ "$UPGRADE" -eq 0 ]] && grep -qF "$MARKER" "$AGENT_CONF" 2>/dev/null; }; then
      _log "  agent.conf.add already appended (idempotent, skipping)"
    elif [[ "$MIGRATION" -eq 1 ]]; then
      {
        echo ""
        echo "$CONF_PAIR_BEGIN"
        echo "$MARKER"
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
        echo "$CONF_PAIR_END"
      } >> "$AGENT_CONF"
      _log "  appended config/agent.conf.add (migration path; peer=$PEER_ROOT; MIGRATION_PEER set)"
    else
      {
        echo ""
        echo "$CONF_PAIR_BEGIN"
        echo "$MARKER"
        echo "# fresh/standalone mint (DGN-284/DGN-227): migration-family keys"
        echo "# (L1_DB / L1_EXPECTED_USER_VERSION / HANDOFF_PEER_AG / MIGRATION_PEER) intentionally omitted"
        # keep any future non-migration keys the fragment may carry
        grep -v '^#' "$CONF_ADD" | grep -v '^$' | grep -Ev "$PEER_KEYS_RE" || true
        echo "$CONF_PAIR_END"
      } >> "$AGENT_CONF"
      _log "  appended config/agent.conf.add (fresh path; migration keys omitted)"
    fi
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
    python3 "$LEDGER_PY" apply --db "$DB" 2>&1 | while IFS= read -r line; do _log "  ledger: $line"; done
    _log "step 4: W01 apply done"
  else
    [[ -f "$LEDGER_PY" ]] || _fail "step 4: ledger.py not found at $LEDGER_PY (package copy step 2 failed?)"
    [[ -f "$DB" ]] || _fail "step 4: lifekit.db not found at $DB (fresh mint incomplete?)"
  fi
else
  _log "step 4: ledger apply SKIPPED (pack ships no lib/ledger.py)"
fi

# ---------- STEP 5: hook wiring (settings.json -- idempotent python edit) ---
# Runs only when the pack ships routines/ledger-inject.py.
SETTINGS="$ROOT/.claude/settings.json"
if has_cat routines && [[ -f "$PKG_PAYLOAD/routines/ledger-inject.py" ]]; then
  _log "step 5: ledger-inject hook wiring"

  LEDGER_HOOK_CMD="/usr/bin/python3 $ROOT/routines/ledger-inject.py"

  python3 - "$SETTINGS" "$LEDGER_HOOK_CMD" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
hook_cmd = sys.argv[2]

with open(settings_path) as f:
    s = json.load(f)

hooks = s.setdefault("hooks", {})
ups_list = hooks.setdefault("UserPromptSubmit", [])

# Check if the ledger-inject command is already wired anywhere in UserPromptSubmit
for entry in ups_list:
    # entry may be {"hooks": [...]} or direct {"type": "command", "command": ...}
    if isinstance(entry, dict):
        if entry.get("command") == hook_cmd:
            print("[hook-wire] ledger-inject already wired (idempotent)")
            sys.exit(0)
        for h in entry.get("hooks", []):
            if isinstance(h, dict) and h.get("command") == hook_cmd:
                print("[hook-wire] ledger-inject already wired (idempotent)")
                sys.exit(0)

# Add the hook as a new entry in UserPromptSubmit (same pattern as existing entries)
new_entry = {
    "hooks": [
        {
            "type": "command",
            "command": hook_cmd,
            "timeout": 10
        }
    ]
}
ups_list.append(new_entry)
print("[hook-wire] added ledger-inject to UserPromptSubmit")

with open(settings_path, "w") as f:
    json.dump(s, f, indent=2)
    f.write("\n")
PYEOF
  _log "step 5: hook wiring done"
else
  _log "step 5: hook wiring SKIPPED (pack ships no routines/ledger-inject.py)"
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
          cp -f "$_sf" "$_dst"
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
      [[ -e "$link_path" ]] && { _log "  WARN: $link_path is not a symlink but exists -- removing"; rm -rf "$link_path"; }
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
      _stale_label="$(basename "$stale" .plist)"
      if [[ -n "${DOGANY_LAUNCHD_CAPTURE:-}" ]]; then
        printf 'launchctl bootout gui/UID/%s\n' "$_stale_label" >> "$DOGANY_LAUNCHD_CAPTURE"
        _log "  upgrade: (rehearsal) bootout captured for $_stale_label"
      else
        launchctl bootout "gui/$(id -u)/$_stale_label" >/dev/null 2>&1 || true
        rm -f "$HOME/Library/LaunchAgents/$(basename "$stale")" 2>/dev/null || true
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

# DGN-227 B3/P7: instance consumption record -- DOGANY_PACKS list-form upsert
# in .instance.conf (id@version entries; other packs' entries preserved).
# TK-13 U1: atomic primitive (flock + tmp/fsync/rename) -- same-wire with the
# kit path upsert above (lock L216 mandatory co-repair).
_packs_upsert_atomic "$ROOT/.instance.conf" "$PACK_ID" "$PACK_VERSION"
_log "  .instance.conf DOGANY_PACKS upserted ($PACK_ID@$PACK_VERSION)"

# DGN-1031: requires_framework record for the framework-update-time compat
# gate (update.sh fw_reqframework_guard). Same-wire with the kit path record
# above.
_reqfw_record "$ROOT" "$PACK_ID" "$MANIFEST"

# DGN-227 D1/P18 (replaces DGN-402 grill r2 MAJOR-2 predicate): reconcile
# pack-tagged preserve entries against the INSTALL LEDGER on every
# install/reinstall/upgrade; hand-written (untagged) entries never touched.
_preserve_reconcile

# ---------- STEP 7b: AGENT.md fragment append (rendered, idempotent) --------
if has_cat agent_md_fragment; then
  _log "step 7b: AGENT.md fragment append"

  # DGN-773 R5/T4b: resolve to PROFILE.md (2.0, real file) or AGENT.md
  # (pre-2.0, real file) -- NEVER the AGENT.md symlink path itself, so the
  # excise-and-rename below (line ~2848) renames onto the real target and
  # never touches/clobbers the AGENT.md compat symlink.
  AGENT_MD="$(resolve_persona_md "$ROOT")"
  AGENT_ADD="$PKG_PAYLOAD/AGENT.md.add"

  # DGN-227 D2/P8: BEGIN/END marker-pair block (HTML comment pair for md).
  MD_PAIR_BEGIN="<!-- DOGANY-PACK:$PACK_ID:BEGIN -->"
  MD_PAIR_END="<!-- DOGANY-PACK:$PACK_ID:END -->"

  if [[ -f "$AGENT_ADD" ]]; then
    if [[ ! -f "$AGENT_MD" ]]; then
      _fail "step 7b: AGENT.md not found at $AGENT_MD (mint incomplete?)"
    fi
    if [[ "$UPGRADE" -eq 1 ]] && grep -qF "$MD_PAIR_BEGIN" "$AGENT_MD" 2>/dev/null; then
      # DEST-ADJACENT mktemp (settings.json/AGENT-OPS.md precedent, update.sh
      # 3g/3k2) so the mv below is a same-filesystem rename. cp -p stamps
      # AGENT_MD's mode onto the 0600 mktemp file BEFORE the awk redirect
      # truncates it (sed_inplace precedent, update.sh:232-247) -- otherwise
      # the excise silently regresses the persona file to mode 0600.
      _tmp_md="$(mktemp "$AGENT_MD.excise.XXXXXX")"
      cp -p "$AGENT_MD" "$_tmp_md"
      awk -v b="$MD_PAIR_BEGIN" -v e="$MD_PAIR_END" '
        $0 == b { inblk=1; next }
        $0 == e { inblk=0; next }
        !inblk { print }' "$AGENT_MD" > "$_tmp_md"
      mv "$_tmp_md" "$AGENT_MD"
      _log "  upgrade: excised prior AGENT.md fragment block (marker pair)"
    elif [[ "$UPGRADE" -eq 1 ]] && grep -qF "$AGENT_MARKER" "$AGENT_MD" 2>/dev/null; then
      _fail "step 7b: --upgrade found a LEGACY single-marker AGENT.md fragment (no BEGIN/END pair) -- cannot bound it mechanically. Manual migration: remove the old block, then re-run (loud-FAIL by design, DGN-227 D2)"
    fi
    if grep -qF "$MD_PAIR_BEGIN" "$AGENT_MD" 2>/dev/null \
       || { [[ "$UPGRADE" -eq 0 ]] && grep -qF "$AGENT_MARKER" "$AGENT_MD" 2>/dev/null; }; then
      _log "  AGENT.md fragment already appended (idempotent, skipping)"
    else
      # Render the fragment through the same slug/root substitution as the
      # plists so slug-derived prose lands correctly (DGN-366 L2 step 7b).
      RENDERED_ADD="$(mktemp)"
      _render_to "$AGENT_ADD" "$RENDERED_ADD"
      {
        echo ""
        echo "$MD_PAIR_BEGIN"
        cat "$RENDERED_ADD"
        echo "$MD_PAIR_END"
      } >> "$AGENT_MD"
      rm -f "$RENDERED_ADD"
      _log "  appended AGENT.md.add (rendered, marker pair) to AGENT.md (marker: $AGENT_MARKER)"
    fi
  else
    _log "  no AGENT.md.add in package (skipping)"
  fi

  _log "step 7b: AGENT.md fragment done"
else
  _log "step 7b: AGENT.md fragment SKIPPED (category agent_md_fragment not declared)"
fi

# ---------- STEP 7c: knowledge wiring selftest (DGN-402, zero-model) ---------
# Warehouse packs: gates G1-G4 (delivery / discovery / refraction predicates).
# Warehouse-less packs: inverse check (zero warehouse artifacts). Same script
# is re-run by the agent-crafting phase 2 checklist (single logic home).
# G5 (live probes) stays manual -- the script only prints a reminder.
SELFTEST_SH="$SCRIPT_DIR/knowledge_selftest.sh"
_log "step 7c: knowledge wiring selftest"
[[ -x "$SELFTEST_SH" ]] || _fail "step 7c: knowledge_selftest.sh not found/executable: $SELFTEST_SH"
SELFTEST_OUT="$(mktemp)"
set +e
"$SELFTEST_SH" "$ROOT" --manifest "$MANIFEST" > "$SELFTEST_OUT" 2>&1
SELFTEST_RC=$?
set -e
while IFS= read -r line; do _log "  selftest: $line"; done < "$SELFTEST_OUT"
rm -f "$SELFTEST_OUT"
if [[ "$SELFTEST_RC" -ne 0 ]]; then
  _fail "step 7c: knowledge wiring selftest FAILED (exit $SELFTEST_RC) -- the wiring gates must pass at install time"
fi
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

  python3 - "$DB" "$CONSULT_SEED" <<'PYEOF'
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

python3 - "$SETTINGS" "$MODEL_OPT" <<'PYEOF'
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

# ---------- STEP 10: bot start (mint_run.sh start) --------------------------
if [[ "$NO_START" -eq 1 ]]; then
  _log "step 10: bot start SKIPPED (--no-start)"
else
  _log "step 10: bot start (launchd bootstrap, plists.defer honored)"
  MINT_RUN="$SCRIPT_DIR/mint_run.sh"
  [[ -x "$MINT_RUN" ]] || _fail "step 10: mint_run.sh not found/executable: $MINT_RUN"
  "$MINT_RUN" start --root "$ROOT" 2>&1 | while IFS= read -r line; do _log "  start: $line"; done
  _log "step 10: bot start done"
fi

# ---------- STEP 11: minting_state record (instance-dependent) ---------------
if [[ "$NO_STATE" -eq 1 ]]; then
  _log "step 11: minting_state record SKIPPED (--no-state)"
elif [[ -z "$INSTANCE_ROOT" ]]; then
  _log "step 11: minting_state record SKIPPED (--instance-root not supplied; instance-dependent step -- explicit skip, DGN-366 L1)"
else
  _log "step 11: record mint state (instance-root=$INSTANCE_ROOT)"
  STATE_PY="$INSTANCE_ROOT/.claude/skills-bundle/mint-agent/scripts/minting_state.py"
  if [[ -f "$STATE_PY" ]]; then
    python3 "$STATE_PY" accept "$DOMAIN_FIELD" --agent "$SLUG" 2>&1 | while IFS= read -r line; do _log "  state: $line"; done || true
    _log "step 11: minting_state accept done"
  else
    _log "  minting_state.py not found at $STATE_PY -- skipping accept record (explicit skip)"
  fi
fi

_log "=== pack-install DONE pack=$PACK_ID slug=$SLUG ==="
echo "[pack_install] DONE -- see $LOG_FILE"
