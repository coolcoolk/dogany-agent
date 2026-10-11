#!/bin/bash
# prev_backup_prune.sh -- retention policy for the installer's ".prev-<stamp>"
# backup copies under a crew-shared skills root (DGN-1395).
#
# Sourced function library (pack_install.sh calls prev_backup_prune right
# after it creates a backup), with a direct-execution guard so the same file
# can run the one-time reconcile of copies that predate the pruner. Requires
# python3.
#
# THE DISEASE: _kit_share_skill renames the previous shared body to
# "<skill>.prev-<stamp>" on every materialize/refresh and nothing ever
# removed them. Measured on one crew-shared root: 19 entries = 1 live skill
# + 18 backups accumulated over five days, zero of them referenced by any
# instance symlink.
#
# WHY A RETENTION FLOOR AND NOT A DELETE: the shared root is plain data with
# no version history behind it, so these copies are the only rollback path
# that exists for a hand-landed or hand-enhanced skill body. "Remove them
# all" is therefore not available; the policy keeps a floor and trims the
# tail.
#
# THE PREDICATE (one expression, one place -- both the write-time pruner and
# the reconcile read THIS line and no other):
#
#     keep = (rank < PREV_BACKUP_KEEP_N) or (age_days < PREV_BACKUP_KEEP_DAYS)
#
# rank = position in the newest-first ordering of that skill's backups
# (0 = newest), age_days = distance from the reference time to the stamp the
# backup carries in its own name. The two clauses are OR'd on purpose: the
# count clause survives a burst of installs inside one day, the age clause
# survives a skill that is installed once and then left alone.
#
# WHY THE NAME STAMP AND NOT mtime: the installer creates a backup with
# `mv`, which preserves the moved directory's original mtime -- so mtime is
# the age of the skill BODY, not the age of the backup. Measured offsets of
# several hours between the two on the live root above. The "<stamp>" suffix
# is written by the installer at backup time and is the only honest clock.
#
# WHY THE WRITER PRUNES: enforcement wiring of the scan/sweeper/cron kind is
# refused by standing decision; the one process that creates these copies is
# the one process that removes them, in the same function, under the same
# stamp.
#
# FAIL-SAFE RULES (every one of them is "do not delete"):
#   - only entries whose name is exactly "<skill>.prev-<8 digits>-<6 digits>"
#     are ever candidates; anything else in the root (the live skill dir,
#     ".stage.*", ".pre-share-*", hand-made copies) is invisible here;
#   - an entry whose stamp does not parse as a real timestamp is dropped from
#     the candidate set entirely -- it can neither be ranked nor aged, so it
#     is never removed;
#   - an unreadable / missing root yields an empty selection, not an error;
#   - removal failures are reported and never abort the caller.

PREV_BACKUP_KEEP_N=3
PREV_BACKUP_KEEP_DAYS=7

# DGN-1424: shared python3 resolver (PATH-independent -- see the lib file
# header for why a bare `python3` call is unsafe on a human's own terminal).
_PY_RESOLVE_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/py_resolve.sh"
# shellcheck source=py_resolve.sh
. "$_PY_RESOLVE_LIB"

# prev_backup_select <shared_root> <skill_name|--all> [<now_stamp>]
# Prints the absolute path of every backup the predicate does NOT keep, one
# per line, newest-first ordering applied per skill name. <now_stamp> is the
# reference time in the installer's own "%Y%m%d-%H%M%S" form; omitted or
# unparseable means "right now".
prev_backup_select() {
  local root="$1" name="$2" now="${3:-}"
  [[ -n "$root" && -d "$root" ]] || return 0
  pack_resolve_py3 || return 1
  "$PACK_PY3" - "$root" "$name" "$now" "$PREV_BACKUP_KEEP_N" "$PREV_BACKUP_KEEP_DAYS" <<'PYEOF'
import os, re, sys, time
from datetime import datetime

root, want, now_raw, keep_n, keep_days = sys.argv[1:6]
keep_n = int(keep_n)
keep_days = float(keep_days)
STAMP = re.compile(r"^(?P<base>.+)\.prev-(?P<stamp>[0-9]{8}-[0-9]{6})\Z")
FMT = "%Y%m%d-%H%M%S"


def epoch(stamp):
    try:
        return datetime.strptime(stamp, FMT).timestamp()
    except ValueError:
        return None


now = epoch(now_raw) if now_raw else None
if now is None:
    now = time.time()

try:
    entries = os.listdir(root)
except OSError:
    sys.exit(0)

by_base = {}
for entry in entries:
    m = STAMP.match(entry)
    if not m:
        continue
    base = m.group("base")
    if want != "--all" and base != want:
        continue
    when = epoch(m.group("stamp"))
    if when is None:
        # Unparseable stamp: not rankable, not ageable -- never a candidate.
        continue
    by_base.setdefault(base, []).append((m.group("stamp"), when, entry))

for base in sorted(by_base):
    ordered = sorted(by_base[base], key=lambda row: row[0], reverse=True)
    for rank, (_stamp, when, entry) in enumerate(ordered):
        age_days = (now - when) / 86400.0
        # THE PREDICATE -- the only place it is written.
        keep = (rank < keep_n) or (age_days < keep_days)
        if not keep:
            print(os.path.join(root, entry))
PYEOF
}

# prev_backup_prune <shared_root> <skill_name|--all> [<now_stamp>] [--dry-run]
# Applies the selection. Prints one "pruned: <path>" line per removal (or
# "would prune: <path>" under --dry-run) so the caller owns the log format.
# Never returns non-zero: a backup that cannot be removed is a warning, not
# a reason to fail an install.
prev_backup_prune() {
  local root="$1" name="$2" now="${3:-}" mode="${4:-}"
  local path
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    if [[ "$mode" == "--dry-run" ]]; then
      printf 'would prune: %s\n' "$path"
      continue
    fi
    if rm -rf -- "$path" 2>/dev/null; then
      printf 'pruned: %s\n' "$path"
    else
      printf 'WARN: could not remove stale backup: %s\n' "$path"
    fi
  done < <(prev_backup_select "$root" "$name" "$now")
  return 0
}

# Direct-execution guard: the one-time reconcile of backups that predate the
# write-time pruner. A write-time pruner only ever sees a skill that is being
# installed again, so copies of a skill nobody reinstalls would sit there
# forever; this command applies the SAME predicate to every skill in a root,
# once, by hand. It is deliberately NOT wired into the install path and NOT
# scheduled -- it is an operator command, and it is idempotent, so a second
# run on an already-reconciled root removes nothing.
#
#   prev_backup_prune.sh --reconcile <shared_root> [--dry-run]
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  if [[ "${1:-}" != "--reconcile" || -z "${2:-}" ]]; then
    echo "usage: prev_backup_prune.sh --reconcile <shared_root> [--dry-run]" >&2
    exit 1
  fi
  _root="$2"
  _mode="${3:-}"
  if [[ -n "$_mode" && "$_mode" != "--dry-run" ]]; then
    echo "unknown option: $_mode" >&2
    exit 1
  fi
  if [[ ! -d "$_root" ]]; then
    echo "ERROR: not a directory: $_root" >&2
    exit 1
  fi
  echo "reconcile: root=$_root keep_newest=$PREV_BACKUP_KEEP_N keep_days=$PREV_BACKUP_KEEP_DAYS${_mode:+ (dry-run)}"
  prev_backup_prune "$_root" --all "" "$_mode"
  echo "reconcile: done"
fi
