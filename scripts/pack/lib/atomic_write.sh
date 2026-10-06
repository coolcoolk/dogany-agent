# shellcheck shell=bash
# atomic_write.sh -- whole-file writes into a LIVE instance tree (DGN-1845).
#
# Measured live 2026-10-04 07:51: pack_install's kit_core step delivered
# database/kit_life.py with an in-place `cp -f`; an owner turn importing the
# module mid-copy got a SyntaxError (unterminated triple-quoted string) -- it
# read a half-written file. Every writer here instead fills a temp SIBLING of
# the target (same directory = same filesystem), stamps the mode, then
# renames it over the target with `mv -f` (rename(2)). A concurrent reader
# opens either the complete old file or the complete new one, never a prefix.
# On any failure the temp is removed and the target is left untouched.
#
# Semantics kept from the plain `cp -f` / `>` writers this replaces:
#   - mode: an existing target keeps ITS mode (cp -f / `>` never change the
#     mode of an existing file); a new target gets the source mode (cp) or
#     0666, masked by the umask -- what cp / a shell redirect would create.
#   - a symlink target is written THROUGH (the link's final file is replaced,
#     the link stays), as cp -f / `>` do.
#   - write permission: atomic_cp replaces a read-only target (cp -f unlinks
#     and retries); atomic_write_cmd / atomic_append_cmd REFUSE an existing
#     target that is not writable, as a `>` / `>>` redirect does (a 0444
#     agent.conf must stay a loud append failure, DGN-1467 D9).
# Not kept (by design): the target's inode. A hard-linked target is split.
# Per-file atomicity only -- a whole-tree flip is a separate design (DGN-1030).
#
# Portable: bash 3.2, BSD + GNU mktemp/stat/mv. Library only (no side effects
# at source time). Not reentrant: one atomic_begin..commit at a time.

AW_TMP=""
AW_DST=""
AW_MODE=""

# _aw_redirect_ok <dst> -- 0 when a `>`/`>>` redirect could open <dst>
# (absent, or writable through any symlink).
_aw_redirect_ok() {
  [[ ! -e "$1" || -w "$1" ]]
}

# _aw_mode <file> -- print the permission bits in octal (GNU stat, then BSD).
_aw_mode() {
  stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null
}

# atomic_begin <dst> [<mode-src>] -- resolve <dst> through symlinks, create the
# temp sibling (AW_TMP) and decide the final mode (AW_MODE). rc 1 = nothing
# created. <mode-src> supplies the mode when <dst> does not exist yet.
atomic_begin() {
  local dst="$1" msrc="${2:-}" real l n=0 mode
  AW_TMP=""; AW_DST=""; AW_MODE=""
  real="$dst"
  while [[ -L "$real" ]]; do
    n=$((n + 1))
    [[ "$n" -le 40 ]] || return 1
    l="$(readlink "$real")" || return 1
    case "$l" in
      /*) real="$l" ;;
      *)  real="$(dirname "$real")/$l" ;;
    esac
  done
  if [[ -e "$real" ]]; then
    [[ -f "$real" ]] || return 1
    mode="$(_aw_mode "$real")"
  else
    mode=666
    [[ -n "$msrc" ]] && mode="$(_aw_mode "$msrc")"
    [[ -n "$mode" ]] && mode="$(printf '%o' $(( 0$mode & ~0$(umask) & 0777 )))"
  fi
  [[ -n "$mode" ]] || return 1
  AW_TMP="$(mktemp "$(dirname "$real")/.$(basename "$real").aw.XXXXXX")" || { AW_TMP=""; return 1; }
  AW_DST="$real"
  AW_MODE="$mode"
  return 0
}

# atomic_abort -- drop the temp sibling (target untouched).
atomic_abort() {
  [[ -n "$AW_TMP" ]] && rm -f "$AW_TMP"
  AW_TMP=""
  return 0
}

# atomic_commit -- stamp the mode on the filled temp and rename it over the
# target. The mode is applied last so a read-only mode cannot block the fill.
atomic_commit() {
  if chmod "$AW_MODE" "$AW_TMP" && mv -f "$AW_TMP" "$AW_DST"; then
    AW_TMP=""
    return 0
  fi
  atomic_abort
  return 1
}

# atomic_cp <src> <dst> -- `cp -f <src> <dst>`, atomically. <dst> is a file
# path (not a directory).
atomic_cp() {
  local src="$1" dst="$2"
  [[ -f "$src" ]] || return 1
  atomic_begin "$dst" "$src" || return 1
  cp "$src" "$AW_TMP" || { atomic_abort; return 1; }
  atomic_commit
}

# atomic_write_cmd <dst> <cmd> [args...] -- `<cmd> args > <dst>`, atomically.
# A nonzero <cmd> leaves <dst> untouched.
atomic_write_cmd() {
  local dst="$1"; shift
  _aw_redirect_ok "$dst" || { echo "atomic_write: $dst: Permission denied" >&2; return 1; }
  atomic_begin "$dst" || return 1
  "$@" > "$AW_TMP" || { atomic_abort; return 1; }
  atomic_commit
}

# atomic_append_cmd <dst> <cmd> [args...] -- `<cmd> args >> <dst>`,
# atomically: readers see the file without the appended block or with all of
# it, never a partial block.
atomic_append_cmd() {
  local dst="$1"; shift
  _aw_redirect_ok "$dst" || { echo "atomic_write: $dst: Permission denied" >&2; return 1; }
  atomic_begin "$dst" || return 1
  if [[ -f "$AW_DST" ]]; then
    cat "$AW_DST" > "$AW_TMP" || { atomic_abort; return 1; }
  fi
  "$@" >> "$AW_TMP" || { atomic_abort; return 1; }
  atomic_commit
}
