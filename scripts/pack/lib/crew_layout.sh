# crew_layout.sh -- DGN-1509 SPEC v3 §3.1 / v4 §2.3 (F2) layout-migration
# primitive (S5b: "layout migration" -- built and measured STANDALONE,
# BEFORE any wiring into S7's _apply_crew; spec's own words: "S5b 는 S7 에
# 접지 않는다"). Sourced (no shebang -- a library, not a runnable script).
# Requires $PACK_PY3 to already be resolved by the caller.
#
# WHAT THIS IS. Before this design existed, every generation-scoped
# consumer name under a member's database/ (kit_life.py, agent_conf.py,
# schema.sql, migrations/*.sql, ...) was a plain real file. crew_activate.sh
# (S7 Phase 1) flips database/.gen-active to point at a staged generation,
# but that flip is only OBSERVABLE to a consumer if database/<name> is
# itself a symlink resolving through .gen-active/<name> -- otherwise
# activation changes a symlink nobody reads. This file is that missing
# link: for each name in a generation's candidate.json entries[], convert
# database/<name> from a real file to a permanent symlink
# database/<name> -> .gen-active/<name> (relative, nesting-adjusted),
# moving the real file's content into database/.gen/<txn>/<name> first so
# it becomes that generation's own copy.
#
# WHY THIS IS ITS OWN STEP, NOT FOLDED INTO ACTIVATE. Converting N names is
# a non-atomic multi-file operation (each name is two syscalls: rename(2)
# then symlink(2)); activate's own contract is exactly ONE atomic
# rename(2) call for the WHOLE member. Mixing the two would make activate
# lie about its one atomic guarantee. Spec keeps them separate stages
# (S5b, S7) on purpose.
#
# THE STATE SPACE PER NAME (locked contract, spec v4 §2.2/§2.3 -- the v3
# text this file originally shipped against had only two branches and
# missed a reachable third; v4's recheck confirmed the three-branch table
# below with 7 mutation-equivalent test cases + the same-fs rename
# premise, and v5/v6's rechecks re-affirmed it unchanged):
#
#   branch1  database/<name> is a symlink already.
#              readlink == .gen-active/<name> (nesting-adjusted) -> skip.
#              Any OTHER target -> do not silently walk past it: name the
#              anomaly, INCOMPLETE, do not abort the whole run (an
#              unrelated name's foreign symlink says nothing about the
#              rest of entries[]).
#   branch2  database/<name> is a real file (the common case: never
#              migrated, or migration crashed BEFORE this name's move).
#              rename(2) it into .gen/<txn>/<name>, then fall through to
#              symlink creation below.
#   branch3  database/<name> is ABSENT and .gen/<txn>/<name> IS present.
#              This is the crash window between branch2's move and its
#              symlink creation -- the move already happened; do not
#              re-attempt it (there is nothing left at the old path to
#              move). Create the symlink only.
#   branch4  database/<name> is ABSENT and .gen/<txn>/<name> is ALSO
#              absent. rename(2) is a single atomic syscall, so THIS
#              procedure's own crashes can never produce "neither place
#              has the file" -- reaching branch4 means something outside
#              this procedure's crash model touched the state (hand
#              edit, wrong txn, partial restore from backup). Fail-closed:
#              name it, INCOMPLETE, and ABORT the rest of this run (spec's
#              own words: "추측 복구 금지" -- no guessing a recovery).
#              Names not yet reached are still real files -- a caller can
#              re-check with _crew_layout_mixed_state and either retry
#              (harmless if the branch4 cause was transient) or escalate.
#
# WHY ABORT ONLY ON BRANCH4, NOT ON A BRANCH1 MISMATCH. branch4 signals
# state this procedure cannot explain (external interference); continuing
# to mutate OTHER, unrelated, self-consistent names on top of an
# unexplained anomaly compounds risk for no benefit. A branch1 mismatch on
# one name says nothing about any OTHER name's own state -- refusing to
# touch the mismatched name is already fail-closed for THAT name; the rest
# of entries[] is independent and still safe to converge in the same run.
#
# WHY rename(2) (os.replace), NEVER mv -- same G1 doctrine as
# crew_activate.sh (see that file's header for the macOS BSD mv
# symlink-follow reproduction). Every move in this file goes through
# PACK_PY3's os.replace, and every claimed postcondition is verified by a
# fresh readlink/stat AFTER the call, never by trusting the call's own rc
# (spec §3.0 rule).
#
# WHY THE REVERSE VERB IS A SINGLE rename(2) PER NAME, NOT unlink-THEN-move.
# rename(2) does not dereference its destination -- it atomically replaces
# WHATEVER dirent currently sits at the destination path (regular file,
# symlink, or nothing) with the source. That means
# os.replace(.gen/<txn>/<name>, database/<name>) recovers a name from
# EVERY state _crew_layout_migrate can leave it in -- correctly-symlinked
# (branch1 end state), absent (branch3's mid-crash window), or even a
# foreign symlink -- in ONE atomic step, with no separate "remove the
# symlink" call and therefore no crash window between removal and
# replacement. See tests/dgn1509_layout_selftest.sh MUT-NAIVE-REVERT for
# the two-step (rm; then copy) alternative actually losing data mid-crash,
# proven by running it, not by reading rename(2)'s man page.
#
# WHAT THIS FILE DELIBERATELY DOES NOT DO (S5b scope note, matching
# crew_activate.sh's own precedent): it is not called from anywhere in
# this repo yet. Wiring it into S7's drain-window sequence, and deciding
# which txn a live _apply_crew run passes it, is out of this step's scope
# by spec's own instruction ("S5b 는 S7 에 접지 않는다") -- that decision
# belongs to S7's own dispatch.

# _crew_layout_rel_target <name>
# Prints the relative symlink target for entries[] name <name>, adjusted
# for nesting depth (e.g. "migrations/001.sql" -> "../.gen-active/
# migrations/001.sql", so the symlink at database/migrations/001.sql
# resolves back up into database/.gen-active/migrations/001.sql). A
# flat name (no "/") needs zero "../" hops since the symlink lives
# directly under database/, a sibling of .gen-active itself.
_crew_layout_rel_target() {
  local name="$1"
  local rest="$name"
  local prefix=""
  while [[ "$rest" == */* ]]; do
    prefix="../$prefix"
    rest="${rest#*/}"
  done
  printf '%s.gen-active/%s' "$prefix" "$name"
}

# _crew_layout_entries <candidate.json>
# Prints candidate.json's entries[], one per line. Caller has already
# confirmed the file exists.
_crew_layout_entries() {
  "$PACK_PY3" -c '
import json, sys
doc = json.load(open(sys.argv[1]))
for e in doc.get("entries", []):
    print(e)
' "$1"
}

# _crew_layout_migrate_one <member_root> <txn> <name>
# One name's conversion, per the branch table above. Returns:
#   0  converged (branch1 skip, or branch2/3 completed through to a
#      verified correct symlink)
#   1  INCOMPLETE, non-fatal to the rest of entries[] (branch1 mismatch,
#      or a move/symlink-creation/postcondition failure)
#   4  INCOMPLETE, fatal signal to the caller: branch4, abort the run
_crew_layout_migrate_one() {
  local root="$1" txn="$2" name="$3"
  local db="$root/database"
  local live="$db/$name"
  local gen_file="$db/.gen/$txn/$name"
  local target cur
  target="$(_crew_layout_rel_target "$name")"

  if [[ -L "$live" ]]; then
    cur="$(readlink "$live" 2>/dev/null || printf '')"
    if [[ "$cur" == "$target" ]]; then
      return 0
    fi
    echo "[crew-layout] $root: INCOMPLETE -- $name is a symlink to unexpected target '$cur' (want '$target') -- refusing to overwrite" >&2
    return 1
  fi

  if [[ -f "$live" ]]; then
    # branch2: real file -- move it into the generation first.
    mkdir -p "$(dirname "$gen_file")" 2>/dev/null
    if ! "$PACK_PY3" -c '
import os, sys
os.replace(sys.argv[1], sys.argv[2])
' "$live" "$gen_file" 2>/dev/null; then
      echo "[crew-layout] $root: INCOMPLETE -- move $name -> .gen/$txn/$name failed" >&2
      return 1
    fi
  elif [[ ! -e "$gen_file" ]]; then
    # branch4: neither place has it. Unreachable via this procedure's own
    # crashes (rename is atomic) -- fail-closed, name it, signal abort.
    echo "[crew-layout] $root: INCOMPLETE -- $name missing in both database/ and .gen/$txn/ -- external interference or txn mismatch, refusing to guess (fail-closed)" >&2
    return 4
  fi
  # else: branch3 -- name absent, generation copy already present (the
  # move already happened in a prior, crashed run). Fall through: symlink
  # creation only, no re-move.

  mkdir -p "$(dirname "$live")" 2>/dev/null
  if ! ln -s "$target" "$live" 2>/dev/null; then
    echo "[crew-layout] $root: INCOMPLETE -- symlink creation failed for $name" >&2
    return 1
  fi

  # Postcondition -- read the result, not the rc (spec §3.0).
  cur="$(readlink "$live" 2>/dev/null || printf '')"
  if [[ "$cur" != "$target" ]]; then
    echo "[crew-layout] $root: INCOMPLETE -- postcondition failed for $name, readlink='$cur' != '$target'" >&2
    return 1
  fi
  return 0
}

# _crew_layout_migrate <member_root> <txn>
# Converts every entries[] name of the staged generation .gen/<txn>/ from
# a real file to a permanent symlink (see branch table above). Idempotent:
# re-running after a full success, or after a partial crash, converges.
# Returns:
#   0  fully converged (every name is now the correct symlink) -- includes
#      the idempotent no-op case where every name already was.
#   1  BLOCKED -- .gen/<txn>/candidate.json missing (generation not
#      staged), zero disk change.
#   2  INCOMPLETE -- one or more names did not converge this run. stderr
#      names every offending name (branch1 mismatches actually attempted,
#      plus, if a branch4 abort fired, every name from that point on that
#      was never attempted -- both are "not yet symlinked" and both are
#      exactly what _crew_layout_mixed_state would also report).
_crew_layout_migrate() {
  local root="$1" txn="$2"
  local db="$root/database"
  local cand="$db/.gen/$txn/candidate.json"

  if [[ ! -f "$cand" ]]; then
    echo "[crew-layout] $root: BLOCKED -- generation not staged ($cand missing), fail-closed" >&2
    return 1
  fi

  local -a entries=()
  local name
  while IFS= read -r name; do
    [[ -n "$name" ]] && entries+=("$name")
  done < <(_crew_layout_entries "$cand")

  local -a bad=()
  local i j n rc
  for ((i = 0; i < ${#entries[@]}; i++)); do
    n="${entries[$i]}"
    rc=0
    _crew_layout_migrate_one "$root" "$txn" "$n" || rc=$?
    [[ $rc -eq 0 ]] && continue
    bad+=("$n")
    if [[ $rc -eq 4 ]]; then
      for ((j = i + 1; j < ${#entries[@]}; j++)); do
        bad+=("${entries[$j]}")
      done
      break
    fi
  done

  if [[ ${#bad[@]} -gt 0 ]]; then
    echo "[crew-layout] $root: INCOMPLETE -- ${#bad[@]} name(s) not converged: ${bad[*]}" >&2
    return 2
  fi
  echo "[crew-layout] $root: converged -- $txn (${#entries[@]} names, all symlinked)" >&2
  return 0
}

# _crew_layout_mixed_state <member_root> <txn>
# Read-only mixed-state detector (spec's own phrase: "혼합 상태 감지 =
# entries[] 대비 심링크 아닌 이름 열거"). Prints, one per line, every
# entries[] name whose database/<name> is NOT currently a symlink with
# exactly the expected relative target. Empty output = fully converged.
# Returns 1 if candidate.json is missing (nothing to check against), 0
# otherwise (independent of whether any names are printed).
_crew_layout_mixed_state() {
  local root="$1" txn="$2"
  local db="$root/database"
  local cand="$db/.gen/$txn/candidate.json"
  [[ -f "$cand" ]] || return 1

  local name live target cur
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    live="$db/$name"
    target="$(_crew_layout_rel_target "$name")"
    if [[ -L "$live" ]]; then
      cur="$(readlink "$live" 2>/dev/null || printf '')"
      [[ "$cur" == "$target" ]] && continue
    fi
    printf '%s\n' "$name"
  done < <(_crew_layout_entries "$cand")
  return 0
}

# _crew_layout_revert_one <member_root> <txn> <name>
# One name's reverse conversion. See the file header for why this is a
# single rename(2) call, not unlink-then-copy. Returns 0 converged
# (includes idempotent skip), 1 INCOMPLETE (non-fatal, independent of
# other names -- revert never aborts the run, every name's revert is a
# self-contained atomic op).
_crew_layout_revert_one() {
  local root="$1" txn="$2" name="$3"
  local db="$root/database"
  local live="$db/$name"
  local gen_file="$db/.gen/$txn/$name"

  if [[ -e "$gen_file" ]]; then
    if [[ -f "$live" && ! -L "$live" ]]; then
      # Both a real file at the live path AND a generation copy exist --
      # never produced by this file's own forward/reverse crash model.
      # Refuse to guess which is authoritative.
      echo "[crew-layout] $root: INCOMPLETE (revert) -- $name: both a real file at database/ and a generation copy exist -- ambiguous, refusing to guess" >&2
      return 1
    fi
    if ! "$PACK_PY3" -c '
import os, sys
os.replace(sys.argv[1], sys.argv[2])
' "$gen_file" "$live" 2>/dev/null; then
      echo "[crew-layout] $root: INCOMPLETE (revert) -- move .gen/$txn/$name -> $name failed" >&2
      return 1
    fi
    if [[ ! -f "$live" || -L "$live" ]]; then
      echo "[crew-layout] $root: INCOMPLETE (revert) -- postcondition failed for $name, not a plain real file after move" >&2
      return 1
    fi
    return 0
  fi

  if [[ -f "$live" && ! -L "$live" ]]; then
    return 0
  fi

  echo "[crew-layout] $root: INCOMPLETE (revert) -- $name: no generation copy at .gen/$txn/$name and database/$name is not a plain real file -- external interference or txn mismatch" >&2
  return 1
}

# _crew_layout_revert <member_root> <txn>
# Reverse of _crew_layout_migrate for the SAME txn: for every entries[]
# name, restore database/<name> to a plain real file. Idempotent, and
# recovers from ANY state _crew_layout_migrate's own crashes can leave
# behind -- not only its fully-converged end state (see file header).
# Returns 0 fully reverted, 1 BLOCKED (candidate.json missing), 2
# INCOMPLETE (one or more names not reverted -- stderr names them all).
_crew_layout_revert() {
  local root="$1" txn="$2"
  local db="$root/database"
  local cand="$db/.gen/$txn/candidate.json"

  if [[ ! -f "$cand" ]]; then
    echo "[crew-layout] $root: BLOCKED -- generation not staged ($cand missing), fail-closed" >&2
    return 1
  fi

  local -a entries=()
  local name
  while IFS= read -r name; do
    [[ -n "$name" ]] && entries+=("$name")
  done < <(_crew_layout_entries "$cand")

  local -a bad=()
  for name in "${entries[@]}"; do
    _crew_layout_revert_one "$root" "$txn" "$name" || bad+=("$name")
  done

  if [[ ${#bad[@]} -gt 0 ]]; then
    echo "[crew-layout] $root: INCOMPLETE -- ${#bad[@]} name(s) not reverted: ${bad[*]}" >&2
    return 2
  fi
  echo "[crew-layout] $root: reverted -- $txn (${#entries[@]} names, all real files)" >&2
  return 0
}
