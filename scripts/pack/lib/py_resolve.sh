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
#   3. Homebrew and python.org 3.1x executables, then versioned executables
#      and finally python3 on PATH. This deliberately avoids a macOS
#      /usr/bin/python3 3.9 taking precedence over an installed modern Python.
#
# The optional first argument is the required major.minor version. Existing
# install/update callers use the default 3.11; mint.sh uses 3.10 because the
# bridge SDK supports that floor.
# Sets and exports PACK_PY3 to the resolved interpreter on success.

_pack_py3_version_ok() {
  local have="$1" want_major="$2" want_minor="$3" maj min
  maj="$(printf '%s' "$have" | cut -d. -f1)"
  min="$(printf '%s' "$have" | cut -d. -f2)"
  [[ -z "$maj" ]] && return 1
  [[ "$maj" -gt "$want_major" ]] && return 0
  [[ "$maj" -eq "$want_major" && "${min:-0}" -ge "$want_minor" ]] && return 0
  return 1
}

pack_resolve_py3() {
  local minimum="${1:-3.11}" want_major want_minor cand ver
  local -a candidates

  if [[ ! "$minimum" =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: invalid Python minimum '$minimum' in interpreter resolver -- fail-closed" >&2
    return 1
  fi
  want_major="${minimum%%.*}"
  want_minor="${minimum#*.}"

  # Already resolved (this process, or inherited from a parent's export) --
  # reuse it rather than re-probing every call site.
  if [[ -n "${PACK_PY3:-}" ]] && command -v "$PACK_PY3" >/dev/null 2>&1; then
    ver="$("$PACK_PY3" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
    _pack_py3_version_ok "$ver" "$want_major" "$want_minor" && return 0
  fi

  candidates=(
    "${DOGANY_PYTHON_BIN:-}"
    /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10
    /usr/local/bin/python3.14 /usr/local/bin/python3.13
    /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3.10
    /Library/Frameworks/Python.framework/Versions/3.14/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.10/bin/python3
    python3.14 python3.13 python3.12 python3.11 python3.10 python3
  )
  for cand in "${candidates[@]}"; do
    [[ -z "$cand" ]] && continue
    command -v "$cand" >/dev/null 2>&1 || continue
    ver="$("$cand" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || true)"
    if _pack_py3_version_ok "$ver" "$want_major" "$want_minor"; then
      PACK_PY3="$(command -v "$cand")"
      export PACK_PY3
      return 0
    fi
  done

  {
    echo "ERROR: no Python ${minimum}+ interpreter found -- fail-closed. Searched, in order:"
    echo "  DOGANY_PYTHON_BIN = ${DOGANY_PYTHON_BIN:-<unset>}"
    echo "  Homebrew/python.org Python 3.1x paths, then python3.14 through python3 (via PATH)"
    echo "Set DOGANY_PYTHON_BIN to a Python ${minimum}+ executable or install a supported Python."
  } >&2
  return 1
}
