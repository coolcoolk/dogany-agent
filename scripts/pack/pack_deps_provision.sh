#!/bin/bash
# pack_deps_provision.sh -- pip-provision a pack's python dependencies into
# the instance RUNTIME interpreters (DGN-850, kit<->pack dependency seam).
#
# A pack may ship a requirements.txt next to (inside) its payload declaring
# python libraries its runtime code needs (first customer: lifekit
# payload/requirements.txt -> holidays>=0.83 for the cadence '!hol'
# exclusion). This script provisions those requirements into the
# interpreters the instance runtime ACTUALLY uses, measured from the
# consumers themselves (not assumed):
#
#   1. /usr/bin/python3           routine-roller.sh hardcodes it (T9 wrapper)
#   2. /opt/homebrew/bin/python3  mirror-poll.sh / mirror-reconcile.sh prefer
#                                 it when executable
#   3. command -v python3         lifekit.sh / routines bundle PATH fallback
#
# There is NO instance runtime venv today -- consumers resolve system or
# homebrew interpreters directly, so provisioning targets their user site
# (pip install --user). Candidates are deduped by device:inode (symlink-safe)
# and venv interpreters are SKIPPED: runtime consumers run venv-less under
# launchd, so a caller's active venv must never capture pack deps.
#
# Contract (DGN-850):
#   deterministic  fixed candidate order + stat dedup; same inputs -> same plan
#   idempotent     pip's own already-satisfied short-circuit; re-run = SKIP
#   logged         every line to <root>/.telegram_bot/logs/pack-deps.log
#                  (and stdout -- pack_install.sh tees into pack-install.log)
#   zero-delta     pack without requirements.txt (or comments-only) -> no-op
#   graceful       provisioning failure NEVER fails the caller: consumers ship
#                  their own fallbacks (lifekit holiday_source falls back to a
#                  static KR table when `holidays` is absent). Exit 0 for every
#                  runtime outcome; exit 1 only for usage errors.
#   own cadence    deps update on their own pip cadence, independent of the
#                  pack version line: re-run with --upgrade to pull newer
#                  releases inside the declared specifiers.
#
# Usage:
#   pack_deps_provision.sh --root <instance-root> --requirements <file>
#                          [--upgrade] [--dry-run]
#
# Env (test seam / operator escape hatch):
#   DOGANY_DEPS_PYTHONS        colon-separated interpreter list; REPLACES the
#                              default candidate set when non-empty
#   DOGANY_DEPS_LOCK_RETRIES   lock acquisition attempts (default 12)
#   DOGANY_DEPS_LOCK_INTERVAL  seconds between attempts (default 5)
set -u

ROOT="" REQ="" DRY=0 UPGRADE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root)         ROOT="$2"; shift 2 ;;
    --requirements) REQ="$2"; shift 2 ;;
    --upgrade)      UPGRADE=1; shift ;;
    --dry-run|--dry) DRY=1; shift ;;
    *) echo "usage: pack_deps_provision.sh --root <instance-root> --requirements <file> [--upgrade] [--dry-run]" >&2; exit 1 ;;
  esac
done

[[ -n "$ROOT" ]] || { echo "ERROR: --root required" >&2; exit 1; }
[[ -n "$REQ" ]]  || { echo "ERROR: --requirements required" >&2; exit 1; }
[[ -d "$ROOT" ]] || { echo "ERROR: instance root not found: $ROOT" >&2; exit 1; }

LOG_DIR="$ROOT/.telegram_bot/logs"
LOG_FILE="$LOG_DIR/pack-deps.log"

_log() {
  local ts line
  ts="$(date '+%Y-%m-%dT%H:%M:%S')"
  line="deps: $1"
  echo "$line"
  if [[ "$DRY" -eq 0 ]]; then
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    echo "[$ts] $line" >> "$LOG_FILE" 2>/dev/null || true
  fi
}

# ---------- zero-delta gate: requirements absent or empty --------------------
if [[ ! -f "$REQ" ]]; then
  _log "no-op: requirements file absent ($REQ) -- pack declares no python deps"
  exit 0
fi
if ! grep -v '^[[:space:]]*#' "$REQ" | grep -q '[^[:space:]]'; then
  _log "no-op: requirements file has no requirement lines ($REQ)"
  exit 0
fi

# ---------- candidate resolution (consumer parity, fixed order) --------------
CANDIDATES=()
if [[ -n "${DOGANY_DEPS_PYTHONS:-}" ]]; then
  IFS=':' read -r -a CANDIDATES <<< "$DOGANY_DEPS_PYTHONS"
  _log "candidates overridden via DOGANY_DEPS_PYTHONS (${#CANDIDATES[@]} entries)"
else
  [[ -x /usr/bin/python3 ]] && CANDIDATES+=(/usr/bin/python3)
  [[ -x /opt/homebrew/bin/python3 ]] && CANDIDATES+=(/opt/homebrew/bin/python3)
  _path_py="$(command -v python3 2>/dev/null || true)"
  [[ -n "$_path_py" ]] && CANDIDATES+=("$_path_py")
fi

# _ident <path> -- symlink-following device:inode identity for dedup.
_ident() {
  stat -f '%d:%i' -L "$1" 2>/dev/null || stat -c '%d:%i' -L "$1" 2>/dev/null || echo "path:$1"
}

TARGETS=()
SEEN_IDS=""
for _c in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
  [[ -n "$_c" ]] || continue
  if [[ ! -x "$_c" ]]; then
    _log "candidate skipped (not executable): $_c"
    continue
  fi
  _id="$(_ident "$_c")"
  case " $SEEN_IDS " in
    *" $_id "*) _log "candidate deduped (same interpreter): $_c"; continue ;;
  esac
  SEEN_IDS="$SEEN_IDS $_id"
  # venv guard: runtime consumers run venv-less; never capture a caller venv.
  if ! "$_c" -c 'import sys; sys.exit(1 if sys.prefix != getattr(sys, "base_prefix", sys.prefix) else 0)' 2>/dev/null; then
    _log "candidate skipped (venv interpreter, not a runtime target): $_c"
    continue
  fi
  TARGETS+=("$_c")
done

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  _log "DEGRADE: no runtime interpreter target resolved -- deps NOT provisioned; consumers fall back (graceful, exit 0)"
  exit 0
fi

if [[ "$DRY" -eq 1 ]]; then
  _log "dry-run: requirements=$REQ upgrade=$UPGRADE"
  for _t in "${TARGETS[@]}"; do
    _log "dry-run: would pip-provision (--user) into: $_t"
  done
  exit 0
fi

# ---------- concurrency lock (one provisioner per instance root) -------------
# macOS has no flock(1); python fcntl on an inherited FD (same pattern as
# routine-roller.sh). Lock held by FD 9 for the remainder of this process.
LOCK_FILE="$ROOT/.telegram_bot/pack-deps.lock"
LOCK_RETRIES="${DOGANY_DEPS_LOCK_RETRIES:-12}"
LOCK_INTERVAL="${DOGANY_DEPS_LOCK_INTERVAL:-5}"
LOCK_PY="${TARGETS[0]}"
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
exec 9>"$LOCK_FILE" 2>/dev/null || {
  _log "WARN: cannot open lock file $LOCK_FILE -- proceeding unlocked (loud)"
}
_locked=0
_try=0
while [[ "$_try" -lt "$LOCK_RETRIES" ]]; do
  if "$LOCK_PY" -c 'import fcntl,sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' 9>&9 2>/dev/null <&9; then
    _locked=1
    break
  fi
  _try=$((_try + 1))
  [[ "$_try" -lt "$LOCK_RETRIES" ]] && sleep "$LOCK_INTERVAL"
done
if [[ "$_locked" -ne 1 ]]; then
  _log "DEGRADE: another provisioner holds the lock ($LOCK_FILE) after ${LOCK_RETRIES} attempts -- skipping this run (re-run picks it up; consumers fall back meanwhile)"
  exit 0
fi

# ---------- provision each target --------------------------------------------
N_INSTALLED=0 N_SATISFIED=0 N_DEGRADED=0

_pip_install() { # _pip_install <py> [extra-flags...] -- runs pip, echoes output, returns rc
  local py="$1"; shift
  local flags=(install --user --disable-pip-version-check --no-input)
  [[ "$UPGRADE" -eq 1 ]] && flags+=(--upgrade)
  "$py" -m pip "${flags[@]}" "$@" -r "$REQ" 2>&1
}

for PY in "${TARGETS[@]}"; do
  _log "target: $PY"
  if ! "$PY" -m pip --version >/dev/null 2>&1; then
    _log "  DEGRADE: pip unavailable for $PY -- target skipped (consumers fall back)"
    N_DEGRADED=$((N_DEGRADED + 1))
    continue
  fi

  _out="$(_pip_install "$PY")"; _rc=$?

  # PEP 668 externally-managed interpreter (e.g. homebrew python >= 3.12)
  # refuses even --user installs. Retry once with --break-system-packages:
  # combined with --user this writes ONLY to the user site-packages and never
  # touches the managed prefix (the marker's own suggested remedy for
  # per-user installs). Loud retry, never silent.
  if [[ $_rc -ne 0 ]] && grep -qi 'externally-managed-environment' <<< "$_out"; then
    _log "  externally-managed interpreter -- retrying with --break-system-packages (user site only)"
    _out="$(_pip_install "$PY" --break-system-packages)"; _rc=$?
  fi

  if [[ $_rc -ne 0 ]]; then
    _log "  DEGRADE: pip install failed rc=$_rc for $PY -- deps NOT provisioned there; consumers fall back (graceful)"
    while IFS= read -r _l; do _log "    pip: $_l"; done < <(tail -n 5 <<< "$_out")
    N_DEGRADED=$((N_DEGRADED + 1))
    continue
  fi

  _installed_line="$(grep -i '^Successfully installed' <<< "$_out" | head -1 || true)"
  if [[ -n "$_installed_line" ]]; then
    _log "  installed: ${_installed_line#Successfully installed }"
    N_INSTALLED=$((N_INSTALLED + 1))
  else
    _log "  SKIP: all requirements already satisfied (idempotent)"
    N_SATISFIED=$((N_SATISFIED + 1))
  fi
done

_log "DONE targets=${#TARGETS[@]} installed=$N_INSTALLED satisfied=$N_SATISFIED degraded=$N_DEGRADED (requirements=$REQ)"
exit 0
