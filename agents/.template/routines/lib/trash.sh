#!/bin/bash
# trash.sh -- reversible delete (v2.7 D3 prototype, HF32a "trash > rm").
#
# The D3 Bash gate rewrites a top-level `rm`/`unlink` to this script, so it
# accepts rm's flags and ignores them: -r/-R/-f/-i/-v/-d/-P, --recursive,
# --force, --verbose, --dir, and `--`.  Each operand is MOVED, never
# deleted:
#   - the platform trash command when present (`trash`, macOS 14+ / brew),
#   - else <instance>/files/_archive/trash/<YYYYMMDD>/<HHMMSS>-XXXXXX/<name>:
#     one fresh directory per operand (mktemp -d), so two operands with the
#     same basename never meet (grill F15: the old flat name made the second
#     `mv` overwrite the first, rc 0, data gone), and `mv -n` never
#     overwrites.  cleanup-files.sh purges dated dirs after
#     TRASH_RETENTION_DAYS (30).
# Cross-device (grill F16): an operand on another volume than the archive is
# REFUSED (rc 1, nothing moved) -- a copy across volumes is slow, can fill
# the disk and leaves a half copy when the tool call times out.  Delete it
# with the user's yes instead.
# -f keeps rm's meaning: a missing operand is not an error.
# Exit: 0 all moved; 1 an operand failed (missing without -f, cross-device,
# or move error).
set -u
ROOT="${TRASH_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
force=0; ops=(); end=0
for a in "$@"; do
  if [[ $end -eq 0 ]]; then
    case "$a" in
      --) end=1; continue ;;
      --force) force=1; continue ;;
      --recursive|--verbose|--dir|--interactive) continue ;;
      -[rRfivdIP]*) [[ "$a" == *f* ]] && force=1; continue ;;
    esac
  fi
  ops+=("$a")
done
[[ ${#ops[@]} -eq 0 ]] && exit 0

dev_of() {  # <path> -> device id (GNU stat -c first: BSD stat has no -c)
  stat -c %d "$1" 2>/dev/null || stat -f %d "$1" 2>/dev/null
}

rc=0
dest="$ROOT/files/_archive/trash/$(date +%Y%m%d)"
for p in "${ops[@]}"; do
  if [[ ! -e "$p" && ! -L "$p" ]]; then
    [[ $force -eq 1 ]] || { echo "trash.sh: $p: no such file" >&2; rc=1; }
    continue
  fi
  if [[ -z "${TRASH_NO_PLATFORM:-}" ]] && command -v trash >/dev/null 2>&1; then
    trash "$p" && continue
  fi
  mkdir -p "$dest" || { rc=1; continue; }
  src_dev="$(dev_of "$(dirname -- "$p")")"
  dst_dev="${TRASH_TEST_DEST_DEV:-$(dev_of "$dest")}"
  if [[ -z "$src_dev" || -z "$dst_dev" || "$src_dev" != "$dst_dev" ]]; then
    echo "trash.sh: $p: on another volume than $dest -- not copied; delete it with the user's yes instead" >&2
    rc=1
    continue
  fi
  slot="$(mktemp -d "$dest/$(date +%H%M%S)-XXXXXX")" || { rc=1; continue; }
  mv -n -- "$p" "$slot/" || { rc=1; continue; }
  # mv -n exits 0 even when it skipped: the source must be gone
  if [[ -e "$p" || -L "$p" ]]; then
    echo "trash.sh: $p: not moved" >&2
    rc=1
  fi
done
exit $rc
