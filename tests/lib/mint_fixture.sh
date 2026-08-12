#!/bin/bash
# mint_fixture.sh -- shared REAL-mint sandbox fixture (DGN-674 T1).
#
# Closes the DGN-227 gap where the rehearsal harness STUBBED mint.sh
# (tests/rehearsal_dgn227.sh:8): consumers of this fixture run the REAL
# scripts/mint.sh into a throwaway sandbox, so fresh-mint integrity is
# exercised, not simulated. Owned by DGN-674; consumed by the install smoke
# (tests/install_smoke.sh) and the backup/rollback suites (DGN-672/673).
#
# Contract (source this file, then call):
#   mint_sandbox <workdir> [extra mint args...]
#     - creates <workdir>/home as FAKE_HOME and exports HOME to it
#     - exports a FAKE-SHAPE bot token (never a real secret) via
#       DOGANY_BOT_TOKEN
#     - runs the real scripts/mint.sh:
#         --root <workdir>/inst --name smokebot --core-only --lang ko
#         --owner-id 11111111 --tz Asia/Seoul [extra args]
#     - on success sets MINT_INST (absolute instance root), MINT_FAKE_HOME,
#       MINT_REPO_ROOT and returns 0; mint output lands in <workdir>/mint.log
#       (tail echoed to stderr on failure)
#   mint_remint [extra mint args...]
#     - re-runs the SAME mint argv + --force against MINT_INST (idempotent
#       re-mint leg, smoke S10); output appends to <workdir>/remint.log
#   mint_fixture_cleanup
#     - removes every workdir this fixture created; callers usually register
#       `trap mint_fixture_cleanup EXIT`
#
# SAFETY: no launchd unit is ever bootstrapped, no real token, no network
# beyond pip (the venv build), and the exported HOME is the sandbox fake so
# the real user home is never touched. PIP_CACHE_DIR points inside the
# sandbox workdir so the fake HOME stays hermetic while a re-mint in the
# same sandbox reuses downloaded wheels.

# Fake-shape token: numeric bot id + ':' + 35 chars, matching the Telegram
# token SHAPE only. Never a live credential.
MINT_FAKE_TOKEN="1234567890:AAFakeSmokeTokenDGN674AAAAAAAAAAAAA"

MINT_FIXTURE_DIRS=()
MINT_INST=""
MINT_FAKE_HOME=""
MINT_REPO_ROOT=""
MINT_WORK=""

# Default mint argv (single source for mint_sandbox + mint_remint).
MINT_ARGS=(--name smokebot --core-only --lang ko --owner-id 11111111 --tz Asia/Seoul)

mint_sandbox() { # mint_sandbox <workdir> [extra mint args...]
  local work="$1"; shift
  MINT_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  mkdir -p "$work/home"
  MINT_WORK="$(cd "$work" && pwd)"
  MINT_FAKE_HOME="$MINT_WORK/home"
  MINT_FIXTURE_DIRS+=("$MINT_WORK")
  export HOME="$MINT_FAKE_HOME"
  export DOGANY_BOT_TOKEN="$MINT_FAKE_TOKEN"
  export PIP_CACHE_DIR="$MINT_WORK/pip-cache"
  MINT_INST="$MINT_WORK/inst"
  if ! bash "$MINT_REPO_ROOT/scripts/mint.sh" --root "$MINT_INST" \
        "${MINT_ARGS[@]}" "$@" > "$MINT_WORK/mint.log" 2>&1; then
    echo "[mint_fixture] mint.sh FAILED -- log tail ($MINT_WORK/mint.log):" >&2
    tail -30 "$MINT_WORK/mint.log" >&2
    return 1
  fi
  MINT_INST="$(cd "$MINT_INST" && pwd)"
  return 0
}

mint_remint() { # mint_remint [extra mint args...] -- same argv + --force
  [ -n "$MINT_INST" ] || { echo "[mint_fixture] mint_remint before mint_sandbox" >&2; return 1; }
  if ! bash "$MINT_REPO_ROOT/scripts/mint.sh" --root "$MINT_INST" \
        "${MINT_ARGS[@]}" --force "$@" >> "$MINT_WORK/remint.log" 2>&1; then
    echo "[mint_fixture] re-mint FAILED -- log tail ($MINT_WORK/remint.log):" >&2
    tail -30 "$MINT_WORK/remint.log" >&2
    return 1
  fi
  return 0
}

mint_fixture_cleanup() {
  local d
  for d in ${MINT_FIXTURE_DIRS[@]+"${MINT_FIXTURE_DIRS[@]}"}; do
    [ -n "$d" ] && [ -d "$d" ] && rm -rf "$d"
  done
  MINT_FIXTURE_DIRS=()
}
