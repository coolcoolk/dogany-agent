#!/bin/bash
# DGN-2129: only payload children run concurrently. All shell state belongs
# to the parent; no worker can restart, write the ledger or deliver a result.
_fw_batch_queue() {
  local name="$1" root="$2" from="$3" to="$4" prc=0 saved_n="$UNIT_N"
  # Aliases/duplicate roots are a barrier, never concurrent writers.
  local canon
  canon="$(cd "$root" && pwd -P)" || canon="$root"
  if [[ -n "$FW_BATCH" ]] && awk -F '\t' -v r="$canon" '$7==r {found=1} END {exit !found}' "$FW_BATCH"; then
    _fw_batch_flush
    UNIT_N="$saved_n"
    UNIT_TARGET="$to"
    UNIT_META="framework"$'\x1f'"$name"$'\x1f'$'\x1f'"$name:$root"
  fi
  if _unit_hold "framework $name" "$root"; then
    N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
    _unit_rec "framework $name" blocked "$HOLD_REASON"
    return
  fi
  _fw_precheck "$name" "$root" "$to" || prc=$?
  case "$prc" in
    3) N_OK=$((N_OK + 1)); _unit_rec "framework $name" ok "updated (landed earlier in this same run)"; return ;;
    1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "framework $name" blocked "$FW_BLOCK_REASON"; return ;;
  esac
  if ! _launchd_stop "$root"; then
    N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
    _unit_rec "framework $name" blocked
    return
  fi
  [[ -n "$FW_BATCH" ]] || { FW_BATCH="$TMP/fw.batch.$UNIT_N.tsv"; : > "$FW_BATCH"; }
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$UNIT_N" "$name" "$root" "$to" "$FW_SU" "$FW_NOW" "$canon" >> "$FW_BATCH"
}

_fw_batch_flush() {
  local saved_n="$UNIT_N" n name root to su now canon pool_rc=0
  # Pessimistic before spawn: interruption can never re-arm a writing child.
  while IFS=$'\t' read -r n name root to su now canon; do
    _mark_down "$root"
  done < "$FW_BATCH"
  FW_BATCH_ACTIVE=1
  _phase_begin
  while IFS=$'\t' read -r n name root to su now canon; do
    _progress_event framework "" "$name:$root" running
    _phase_log "$name" "payload-start"
  done < "$FW_BATCH"
  # DGN-2296 fix 2: the pool runs in the background and each member is
  # collected -- and its agent restarted -- the moment ITS payload is done,
  # not when the slowest sibling is. The pool still returns only after every
  # child has exited, and every exit path waits for it (_fw_pool_reap).
  "$PACK_PY3" "$SCRIPT_DIR/lib/update_fw_pool.py" "$FW_BATCH" "$TMP" "$FW_JOBS" &
  FW_POOL_PID=$!
  while kill -0 "$FW_POOL_PID" 2>/dev/null; do
    _fw_batch_collect_ready
    sleep "${DOGANY_APPLY_POOL_POLL:-0.2}"
  done
  _fw_pool_reap || pool_rc=$?
  _fw_batch_collect
  UNIT_N="$saved_n"
  [[ "$pool_rc" -eq 0 ]] || _abort "framework worker pool interrupted or failed (rc=$pool_rc)"
}

# _fw_pool_reap -- join the background pool (TERM it first when asked) and
# return its status; idempotent. Used by the normal path and the EXIT trap so
# no restart or re-arm can run while a payload child is still writing.
_fw_pool_reap() {
  local rc=0
  [[ -n "${FW_POOL_PID:-}" ]] || return 0
  if [[ "${1:-}" == term ]] && kill -0 "$FW_POOL_PID" 2>/dev/null; then
    kill -TERM "$FW_POOL_PID" 2>/dev/null || true
  fi
  wait "$FW_POOL_PID" || rc=$?
  FW_POOL_PID=""
  return "$rc"
}

# _fw_batch_collect_ready -- collect every member whose pool status file is
# already written (the pool publishes it atomically, after the payload exited).
_fw_batch_collect_ready() {
  local n name root to su now canon
  while IFS=$'\t' read -r n name root to su now canon; do
    [[ -f "$TMP/fw.$n.$name.collected" ]] && continue
    # Publish ledger rows in plan order.  A later, faster member may already
    # be ready, but recording it first changes the product result ordering and
    # can make recovery consumers associate a verdict with the wrong member.
    # Stop at the first unfinished predecessor; the next poll will collect it
    # and any ready successors together.
    [[ -s "$TMP/fw.$n.$name.rc" ]] || return 0
    _fw_batch_collect_one "$n" "$name" "$root" "$to" "$su" "$now"
  done < "$FW_BATCH"
}

_fw_batch_collect() {
  local n name root to su now canon
  FW_BATCH_ACTIVE=0
  while IFS=$'\t' read -r n name root to su now canon; do
    [[ -f "$TMP/fw.$n.$name.collected" ]] && continue
    _fw_batch_collect_one "$n" "$name" "$root" "$to" "$su" "$now"
  done < "$FW_BATCH"
  FW_BATCH=""
}

_fw_batch_collect_one() {
  local n="$1" name="$2" root="$3" to="$4" su="$5" now="$6" frc
  : > "$TMP/fw.$n.$name.collected"
  UNIT_N="$n"; FW_NOW="$now"; UNIT_DETAIL=""; UNIT_REFUSAL=""
  UNIT_TARGET="$to"
  UNIT_META="framework"$'\x1f'"$name"$'\x1f'$'\x1f'"$name:$root"
  if [[ -f "$TMP/fw.$n.$name.not-started" ]]; then
    _fw_batch_unmark_down "$root"
    N_UNTRIED=$((N_UNTRIED + 1)); OVERALL_RC=2
    _unit_rec "framework $name" untried "not attempted: update interrupted before this member started"
    _log "  framework $name: not started; stop debt belongs to the parent recovery pass"
    return 0
  fi
  FW_PAYLOAD_RC="$TMP/fw.$n.$name.rc"
  # A missing result is unknown, hence incomplete, never success/re-arm.
  [[ -s "$FW_PAYLOAD_RC" ]] || printf '125\n' > "$FW_PAYLOAD_RC"
  frc=0
  _fw_payload "$name" "$root" "$to" "$su" || frc=$?
  FW_PAYLOAD_RC=""
  if [[ "$frc" -ne 2 ]]; then
    _fw_batch_unmark_down "$root"
  fi
  case "$frc" in
    0) N_OK=$((N_OK + 1)); _unit_rec "framework $name" ok
       _phase_log "$name" "payload-done"
       _fw_defer "$name" "$root" "$to"
       # No later plan line (this batch's flush line included) stops the root
       # again: it is complete NOW, so it comes back now (DGN-2296).
       if ! _touched_from "$root" "$PLAN_LINE_N"; then
         _fw_flush_now "$name" "$root"
       fi ;;
    1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "framework $name" blocked
       _phase_log "$name" "payload-blocked" ;;
    *) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; _unit_rec "framework $name" incomplete
       _phase_log "$name" "payload-incomplete" ;;
  esac
}

_fw_batch_unmark_down() {
  local root="$1" r kept=""
  for r in $DOWN_ROOTS; do
    [[ "$r" == "$root" ]] || kept="${kept:+$kept }$r"
  done
  DOWN_ROOTS="$kept"
}
