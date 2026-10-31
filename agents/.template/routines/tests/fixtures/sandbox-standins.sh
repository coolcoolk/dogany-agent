# fixtures/sandbox-standins.sh -- sourced by dispatch suites (not a suite).
# install_sandbox_standins <bindir>: on a host without the macOS binaries,
# put stand-ins on <bindir> (which the suite prepends to PATH):
#   sandbox-exec  -> sandbox_exec_model.py (profile compiles, self-check probes
#                    judged by the seatbelt model, the command runs UNCONFINED)
#   uuidgen       -> python uuid4
# On macOS nothing is installed: the real kernel stays the judge -- UNLESS
# the kernel refuses to apply ANY profile (sandbox_apply: Operation not
# permitted -- the suite itself runs inside a seatbelt sandbox, e.g. a
# dispatched worker; nested sandboxes are refused). Then the binary exists
# but cannot judge, every spawn self-check fails before a probe runs, and
# the suite would measure the host, not the code: the model stand-in is
# installed exactly as off macOS, and that is announced on stderr.
# SB_STUB=1: the host has NO sandbox-exec (stand-in installed; moving it off
#   PATH reproduces absence). SB_STUB=2: the stand-in SHADOWS a real binary
#   the kernel will not apply (moving it off PATH exposes the real one, so
#   absence is not reproducible). SB_STUB=0: the real kernel judges.
install_sandbox_standins() {
  local bin="$1" here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  SB_STUB=0
  local reason=""
  if ! command -v sandbox-exec >/dev/null 2>&1; then
    reason="no sandbox-exec on this host"; SB_STUB=1
  elif ! sandbox-exec -p '(version 1)(allow default)' /usr/bin/true 2>/dev/null; then
    reason="kernel refuses sandbox_apply here (nested sandbox?)"; SB_STUB=2
  fi
  if [ -n "$reason" ]; then
    echo "[sandbox-standins] NOTE: $reason -- profiles judged by sandbox_exec_model.py, commands run UNCONFINED" >&2
    printf '#!/bin/sh\nexec python3 "%s" exec "$@"\n' "$here/sandbox_exec_model.py" > "$bin/sandbox-exec"
    chmod +x "$bin/sandbox-exec"
  fi
  if ! command -v uuidgen >/dev/null 2>&1; then
    printf '#!/bin/sh\nexec python3 -c "import uuid; print(str(uuid.uuid4()).upper())"\n' > "$bin/uuidgen"
    chmod +x "$bin/uuidgen"
  fi
}
