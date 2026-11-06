# py_resolve.sh -- python3 interpreter resolver for the install/update path
# (DGN-1424). Sourced library (no shebang; defining the two functions below
# has no side effect on its own -- resolution only runs when a caller invokes
# pack_resolve_py3).
#
# THE DEFECT THIS CLOSES: every install/update-path script that needed python3
# called the bare word `python3`, which resolves via PATH -- whatever the
# CALLER's shell happens to find first. On a human's own terminal that is
# routinely macOS's bundled /usr/bin/python3 (3.9.6), whose tokenizer can
# truncate a long manifest/kit comment line mid-UTF-8-sequence and die with
# "SyntaxError: Non-UTF-8 code ... no encoding declared" -- aborting the
# install. The framework already requires >=3.11 (install.sh's probe_python);
# a bot survives only because launchd's PATH happens to put homebrew's newer
# python3 first. Pinning that requirement to a PATH-independent resolution
# closes the gap instead of leaving it to whichever interpreter PATH hands
# back first.
#
# SEARCH ORDER (fail-closed, never mutates PATH):
#   1. $PACK_PY3          -- already resolved by this process or a parent
#                            (pack_install.sh exports it before invoking
#                            compat-lint.sh/mint_run.sh/etc as subprocesses).
#   2. $DOGANY_PYTHON_BIN -- install.sh's own probe_python result, passed
#                            down the same way it already pins mint.sh's venv
#                            interpreter (DGN-157).
#   3. python3, python3.13, python3.12, python3.11 on PATH, first one whose
#      reported version is >= 3.11 (same candidate order + floor as
#      install.sh's probe_python, so the two resolvers agree).
# Sets and exports PACK_PY3 to the resolved interpreter on success.

_pack_py3_version_ok() {
  local have="$1" maj min
  maj="$(printf '%s' "$have" | cut -d. -f1)"
  min="$(printf '%s' "$have" | cut -d. -f2)"
  [[ -z "$maj" ]] && return 1
  [[ "$maj" -gt 3 ]] && return 0
  [[ "$maj" -eq 3 && "${min:-0}" -ge 11 ]] && return 0
  return 1
}

pack_resolve_py3() {
  local cand ver

  # Already resolved (this process, or inherited from a parent's export) --
  # reuse it rather than re-probing every call site.
  if [[ -n "${PACK_PY3:-}" ]] && command -v "$PACK_PY3" >/dev/null 2>&1; then
    ver="$("$PACK_PY3" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
    _pack_py3_version_ok "$ver" && return 0
  fi

  for cand in "${DOGANY_PYTHON_BIN:-}" python3 python3.13 python3.12 python3.11; do
    [[ -z "$cand" ]] && continue
    command -v "$cand" >/dev/null 2>&1 || continue
    ver="$("$cand" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
    if _pack_py3_version_ok "$ver"; then
      PACK_PY3="$(command -v "$cand")"
      export PACK_PY3
      return 0
    fi
  done

  {
    echo "ERROR: no Python 3.11+ interpreter found -- fail-closed. Searched, in order:"
    echo "  DOGANY_PYTHON_BIN = ${DOGANY_PYTHON_BIN:-<unset>}"
    echo "  python3, python3.13, python3.12, python3.11 (via PATH=$PATH)"
    echo "Install Python 3.11+ (macOS: brew install python@3.12) and re-run."
  } >&2
  return 1
}
