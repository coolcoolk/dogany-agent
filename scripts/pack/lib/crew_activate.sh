# crew_activate.sh -- DGN-1509 SPEC v3 §3.1 generation-activate primitive
# (S7, Phase 1: built and measured standalone, BEFORE any wiring). Sourced
# (no shebang -- a library, not a runnable script). Requires $PACK_PY3 to
# already be resolved by the caller.
#
# WHAT THIS IS. A member's prepared candidate generation lives at
# database/.gen/<txn>/ (pack_install --prepare, S5 -- already shipped,
# additive, zero callers). Activation is the ONE mutation that makes a
# generation live: swap the symlink database/.gen-active so it points at
# .gen/<txn> instead of whatever it pointed at before. Per spec, every
# generation-scoped consumer name (candidate.json entries[]) is meant to be
# a permanent symlink database/<name> -> .gen-active/<name>, so flipping
# .gen-active atomically flips every one of those names at once.
#
# WHY rename(2), NEVER mv (G1 -- spec v2/v3 §3.0). This series' oldest
# defect: macOS BSD `mv -f <symlink> <existing-symlink>` follows the
# DESTINATION symlink into the directory it points at and moves the source
# INSIDE it, instead of replacing the destination symlink itself -- rc=0,
# .gen-active unchanged, and the would-be-new generation's symlink is left
# orphaned one level down. `mv -f` NAMES rename(2) in a comment; it does not
# CALL rename(2) on this platform for this exact source/dest shape. GNU
# mv's `-T` (no-target-directory) flag, which would fix this, does not
# exist on macOS BSD mv. os.replace() in Python IS a direct rename(2) call
# (no shell indirection, no "does this look like a directory" heuristic) --
# see tests/dgn1509_activate_selftest.sh MUT-G1 for the reproduction of the
# mv failure AND the os.replace success, both executed (not read) on this
# platform, in this session.
#
# WHY A POSTCONDITION, NOT AN RC CHECK (spec §3.0 rule). G1 IS a fail-silent
# rc=0. A primitive that claims atomicity is proven by reading the RESULT
# state after it runs, never by trusting its own exit code. Every caller of
# _crew_activate_generation gets rc 0/1/2 for control flow, but the
# function's OWN internal decision about whether activation happened is
# always a fresh readlink, never the rename call's rc.
#
# WHAT THIS FILE DELIBERATELY DOES NOT DO (S7 Phase 1 scope note, matching
# crew_lease.sh's own precedent of shipping only what has a caller and a
# test): it does not create the permanent database/<name> -> .gen-active/
# <name> symlinks -- that conversion (real file -> symlink, per-name,
# idempotent, crash-recoverable) is spec's OWN separately staged S5b
# ("layout migration"), explicitly NOT folded into S7 ("S5b ... S7 에 접지
# 않는다"). Without S5b, flipping .gen-active changes nothing a live
# consumer reads -- database/<kit>.py etc. are still real files today (see
# this dispatch's final report for the read-only `ls -la` evidence). This
# file activates .gen-active correctly and provably; it does not, by
# itself, make that flip observable to kit_life.py or any other consumer.
# _apply_crew is NOT wired to call this yet -- see the final report for why.

# _crew_activate_generation <member_root> <txn>
# <txn> is the candidate directory NAME under database/.gen/ (e.g.
# "pack/kit_life/v2.20.1" -- same string pack_install --prepare used as
# --source-tag, matching database/.gen/<txn>/candidate.json's own "txn"
# field). Returns:
#   0  activated (readlink .gen-active == .gen/<txn> confirmed) OR already
#      was active on this exact txn (idempotent no-op, same convention as
#      --prepare's "re-preparing the SAME candidate is not an error")
#   1  BLOCKED, pre-entry-ish -- no disk change to .gen-active. Either the
#      generation was never staged, or the ln/rename step itself failed
#      (ENOSPC, EACCES, or .gen-active already exists as a non-empty real
#      directory -- interference, never silently walked into).
#   2  INCOMPLETE -- rename() returned rc=0 but the postcondition readlink
#      does not match. This is the G1 shape: it must never happen with
#      os.replace on a sane POSIX rename(2), and the mutation-check test
#      (MUT-G1) proves the postcondition catches it the one way it CAN
#      happen (mv reintroduced into this function).
_crew_activate_generation() {
  local root="$1" txn="$2"
  local db="$root/database"
  local gen_dir="$db/.gen/$txn"
  local active="$db/.gen-active"
  local active_new="$db/.gen-active.new"
  local target=".gen/$txn"
  local cur

  if [[ ! -d "$gen_dir" || ! -f "$gen_dir/candidate.json" ]]; then
    echo "[crew-activate] $root: BLOCKED -- generation not staged ($gen_dir/candidate.json missing), fail-closed -- run pack_install --prepare first" >&2
    return 1
  fi

  cur="$(readlink "$active" 2>/dev/null || printf '')"
  if [[ "$cur" == "$target" ]]; then
    echo "[crew-activate] $root: already active on $txn -- idempotent no-op" >&2
    return 0
  fi

  # A crashed prior attempt (crash point 2, "after ln, before rename") can
  # leave .gen-active.new behind. Single-writer per member (the lease
  # protects that) -- safe to clobber, same doctrine as prepare's own
  # .tmp-prepare.$$ cleanup.
  rm -f "$active_new"
  if ! ln -s "$target" "$active_new" 2>/dev/null; then
    echo "[crew-activate] $root: BLOCKED -- ln -s $target $active_new failed (fail-closed, $active untouched)" >&2
    return 1
  fi

  if ! "$PACK_PY3" -c '
import os, sys
os.replace(sys.argv[1], sys.argv[2])
' "$active_new" "$active" 2>/dev/null; then
    rm -f "$active_new"
    echo "[crew-activate] $root: BLOCKED -- rename($active_new -> $active) failed (fail-closed, $active unchanged)" >&2
    return 1
  fi

  # Postcondition -- read the result, not the rc (spec §3.0).
  cur="$(readlink "$active" 2>/dev/null || printf '')"
  if [[ "$cur" != "$target" ]]; then
    echo "[crew-activate] $root: INCOMPLETE -- postcondition failed, readlink($active)='$cur' != '$target' (rename rc was 0 -- this is the G1 shape)" >&2
    return 2
  fi
  echo "[crew-activate] $root: activated $txn (readlink $active == $target)" >&2
  return 0
}
