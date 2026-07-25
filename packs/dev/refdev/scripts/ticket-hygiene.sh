#!/bin/bash
# ticket-hygiene.sh -- weekly ticket hygiene sweep (DGN-409 D, dev pack)
#
# Scans worklog/<PREFIX>-*.md for finding types:
#   A) done candidate: status open|wip AND has at least one checkbox AND zero unchecked - [ ] boxes
#   B) stale candidate: status open AND updated date > 14 days before today
#   C) stale wip: status wip AND updated > 3 days ago
#   D) gate-clearance: parked ticket whose gate_deps IDs are all done -> unpark candidate
#      D-cond: gate_deps all done but gate_cond present -> "cond-remaining" (not full clear)
# Also always emits a big-rock section: table of all P1 tickets (any status except done).
# Also emits lint findings:
#   Rule 6: parked with no gate_deps AND no legacy gate: -> violation
#   Rule 6-depr: parked with legacy gate: prose (no gate_deps) -> deprecated-pass (counted)
#   Lint-minor: gate_deps references a ticket ID that has no matching file
#   Lint-minor: circular reference in gate_deps
#
# The unpark signal is PERSISTENT: every run regenerates worklog/_UNPARK.md
# idempotently from ticket real state (scan = sole writer).
# The ledger header carries a last-scan timestamp so a dead scan shows as staleness.
#
# Weekly (default) run: pushes the A/B/C hygiene digest + big-rock + lint.
#   Push fires if (findings) OR (big-rock non-empty) OR (Rule-6 violation).
# --test: run scan and push regardless; prefix body with [TEST]; if zero say so.
# --dry-run: print would-be push body to stdout; also SKIP the ledger write.
# --gates-only: regenerate the _UNPARK ledger ONLY -- NO push (daily lightweight
#   cron; the persistent ledger is the signal).
#
# Configuration (set these in the calling environment or edit defaults below):
#   TICKET_PREFIX  -- ticket id prefix (default: derived from agent slug in
#                     config/agent.conf SLUG= or fallback "TKT")
#   PUSH_CMD       -- command to push a text body (receives --text <body>).
#                     Default: unset -> dry-run fallback (no push, print only).
#   LOG_FILE       -- path for stdout log (default: /tmp/ticket-hygiene.log)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKLOG="$ROOT/worklog"
DECISIONS_DIR="$WORKLOG/decisions"
UNPARK_LEDGER="$WORKLOG/_UNPARK.md"
LOG_FILE="${LOG_FILE:-/tmp/ticket-hygiene.log}"

# Derive ticket prefix from config/agent.conf SLUG= if available.
# Slug letters only (strip digits/hyphens), first 3 uppercased.
_derive_prefix() {
  local slug=""
  local conf="$ROOT/config/agent.conf"
  if [ -f "$conf" ]; then
    slug="$(grep -m1 '^SLUG=' "$conf" 2>/dev/null | sed 's/^SLUG=//' | tr -d '"' || true)"
  fi
  if [ -z "$slug" ]; then
    slug="$(basename "$ROOT")"
  fi
  # Extract letters only, take first 3 uppercase
  local letters
  letters="$(echo "$slug" | tr -cd 'a-zA-Z' | head -c3 | tr '[:lower:]' '[:upper:]')"
  [ -n "$letters" ] && echo "$letters" || echo "TKT"
}

TICKET_PREFIX="${TICKET_PREFIX:-$(_derive_prefix)}"

# Push command: if PUSH_CMD is unset, fall back to printing (dry-run behavior).
# To wire a real push, set PUSH_CMD="<path-to-push.sh>" in the calling env.
PUSH_CMD="${PUSH_CMD:-}"

TEST_MODE=0
DRY_RUN=0
GATES_ONLY=0
for arg in "${@:-}"; do
  [ "$arg" = "--test" ] && TEST_MODE=1
  [ "$arg" = "--dry-run" ] && DRY_RUN=1
  [ "$arg" = "--gates-only" ] && GATES_ONLY=1
done

log() { echo "[ticket-hygiene] $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE" >&2; }

# --- date arithmetic: today as epoch seconds (macOS + Linux compatible) ---
today_epoch() {
  date +%s
}
date_epoch() {
  # $1 = YYYY-MM-DD
  date -j -f '%Y-%m-%d' "$1" +%s 2>/dev/null \
    || date -d "$1" +%s 2>/dev/null \
    || echo ""
}

NOW="$(today_epoch)"
THRESHOLD_DAYS=14
THRESHOLD_SEC=$((THRESHOLD_DAYS * 86400))
STALE_WIP_DAYS=3
STALE_WIP_SEC=$((STALE_WIP_DAYS * 86400))

# Temp dir for this run
TMPDIR_RUN="$(mktemp -d /tmp/ticket-hygiene.XXXXXX)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

# ---------------------------------------------------------------------------
# Phase 0: build id->file map from frontmatter id: fields
#
# Every <PREFIX>-*.md file carries an "id:" frontmatter field.
# The canonical ticket for <PREFIX>-NNN is the file whose frontmatter id:
# == "<PREFIX>-NNN" -- first matching file wins.
#
# Map stored as tab-delimited file: ID<TAB>STATUS<TAB>FILEPATH
# ---------------------------------------------------------------------------

ID_MAP_FILE="$TMPDIR_RUN/id_map.tsv"
ALL_KNOWN_IDS="$TMPDIR_RUN/known_ids.txt"

log "phase 0: building id->file map (prefix=${TICKET_PREFIX})..."
for _f in "$WORKLOG"/${TICKET_PREFIX}-*.md; do
  [ -f "$_f" ] || continue
  _id=""
  _st=""
  _in=0
  _end=0
  while IFS= read -r _line; do
    if [ "$_in" -eq 0 ]; then
      [ "$_line" = "---" ] && _in=1
      continue
    fi
    [ "$_end" -eq 1 ] && break
    if [ "$_line" = "---" ]; then
      _end=1
      break
    fi
    case "$_line" in
      id:*)
        _id="${_line#id:}"
        _id="${_id#"${_id%%[! ]*}"}"
        _id="${_id%"${_id##*[! ]}"}"
        ;;
      status:*)
        _st="${_line#status:}"
        _st="${_st#"${_st%%[! ]*}"}"
        _st="${_st%"${_st##*[! ]}"}"
        ;;
    esac
  done < "$_f"
  # Register only if id looks like PREFIX-NNN and we haven't seen it yet
  case "$_id" in
    ${TICKET_PREFIX}-[0-9]*)
      if ! grep -qE "^${_id}	" "$ID_MAP_FILE" 2>/dev/null; then
        printf '%s\t%s\t%s\n' "$_id" "$_st" "$_f" >> "$ID_MAP_FILE"
        echo "$_id" >> "$ALL_KNOWN_IDS"
      fi
      ;;
  esac
done
_map_count="$(wc -l < "$ID_MAP_FILE" 2>/dev/null | tr -d ' ' || echo 0)"
log "phase 0: ${_map_count} canonical tickets indexed"

# ---------------------------------------------------------------------------
# Helper: check if a ticket has status: done
# Looks up the prebuilt id->status map; returns 0 if done, 1 if not/missing.
# ---------------------------------------------------------------------------
ticket_is_done() {
  local id="$1"
  local line
  line="$(grep -m1 "^${id}	" "$ID_MAP_FILE" 2>/dev/null || true)"
  [ -z "$line" ] && return 1
  local st
  st="$(echo "$line" | awk -F'\t' '{print $2}')"
  [ "$st" = "done" ]
}

ticket_known() {
  local id="$1"
  grep -qm1 "^${id}	" "$ID_MAP_FILE" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Helper: check if a dec-NNN is decided
# Predicate: decisions/dec-NNN.md EXISTS AND dec-NNN is NOT an active line
# in worklog/_DECISIONS.md.
# ---------------------------------------------------------------------------
dec_is_done() {
  local id="$1"  # e.g. dec-048
  local num="${id#dec-}"
  local dec_file="${DECISIONS_DIR}/dec-${num}.md"

  [ -f "$dec_file" ] || return 1

  local decisions_file="${WORKLOG}/_DECISIONS.md"
  if [ -f "$decisions_file" ]; then
    if grep -qE "^-[[:space:]]+\[.*\][[:space:]]*\[?${id}\]?" "$decisions_file" 2>/dev/null; then
      return 1  # still active (pending user decision)
    fi
  fi
  return 0  # file exists and not in active queue -> decided
}

trim() {
  local s="$1"
  s="${s#"${s%%[! ]*}"}"
  s="${s%"${s##*[! ]}"}"
  echo "$s"
}

# ---------------------------------------------------------------------------
# Circular reference detection helper
# ---------------------------------------------------------------------------
_check_circular() {
  local origin="$1"
  local current="$2"
  local depth="${3:-0}"
  [ "$depth" -gt 10 ] && return 1

  local cur_line
  cur_line="$(grep -m1 "^${current}	" "$ID_MAP_FILE" 2>/dev/null || true)"
  [ -z "$cur_line" ] && return 1
  local cur_f
  cur_f="$(echo "$cur_line" | awk -F'\t' '{print $3}')"
  [ -f "$cur_f" ] || return 1

  local cur_deps=""
  local _in=0 _end=0
  while IFS= read -r _line; do
    if [ "$_in" -eq 0 ]; then
      [ "$_line" = "---" ] && _in=1
      continue
    fi
    [ "$_end" -eq 1 ] && break
    if [ "$_line" = "---" ]; then _end=1; break; fi
    case "$_line" in
      gate_deps:*)
        cur_deps="${_line#gate_deps:}"
        cur_deps="${cur_deps#"${cur_deps%%[! ]*}"}"
        cur_deps="${cur_deps%"${cur_deps##*[! ]}"}"
        ;;
    esac
  done < "$cur_f"

  [ -z "$cur_deps" ] && return 1

  local ref
  for ref in $(echo "$cur_deps" | grep -oE "${TICKET_PREFIX}-[0-9]+" || true); do
    [ "$ref" = "$origin" ] && return 0
    if _check_circular "$origin" "$ref" $(( depth + 1 )); then
      return 0
    fi
  done
  return 1
}

# ---------------------------------------------------------------------------
# Lint state: Rule 6 violations + deprecated-pass + minor findings
# ---------------------------------------------------------------------------
LINT_R6_VIOLS=()
LINT_DEPR_PASS=()
LINT_UNKNOWN_IDS=()
LINT_CIRCULAR=()

# ---------------------------------------------------------------------------
# Phase 1: main scan
# ---------------------------------------------------------------------------
FIND_FILES=()
FIND_TYPES=()
FIND_TITLES=()

BR_IDS=()
BR_STATUSES=()
BR_AGES=()
BR_GATES=()

GC_IDS=()
GC_GATES=()
GC_COND_REMAINING=()
GC_PRIORITY=()

for f in "$WORKLOG"/${TICKET_PREFIX}-*.md; do
  [ -f "$f" ] || continue

  # Sub-document filter: derive ticket id from filename, check against id map.
  bname="$(basename "$f" .md)"
  _pfx="${bname%%-*}"
  _rest="${bname#*-}"
  _num="${_rest%%-*}"
  ticket_id="${_pfx}-${_num}"

  registered="$(grep -m1 "^${ticket_id}	" "$ID_MAP_FILE" 2>/dev/null | awk -F'\t' '{print $3}' || true)"
  if [ -n "$registered" ]; then
    [ "$f" = "$registered" ] || continue
  fi

  # Extract frontmatter fields
  status_val=""
  updated_val=""
  title_val=""
  priority_val=""
  created_val=""
  gate_val=""
  gate_deps_val=""
  gate_cond_val=""
  in_front=0
  front_end=0
  while IFS= read -r line; do
    if [ "$in_front" -eq 0 ]; then
      [ "$line" = "---" ] && in_front=1
      continue
    fi
    [ "$front_end" -eq 1 ] && break
    if [ "$line" = "---" ]; then
      front_end=1
      break
    fi
    case "$line" in
      status:*)     status_val="$(trim "${line#status:}")" ;;
      updated:*)    updated_val="$(trim "${line#updated:}")" ;;
      title:*)      title_val="$(trim "${line#title:}")" ;;
      priority:*)   priority_val="$(trim "${line#priority:}")" ;;
      created:*)    created_val="$(trim "${line#created:}")" ;;
      gate:*)       gate_val="$(trim "${line#gate:}")" ;;
      gate_deps:*)  gate_deps_val="$(trim "${line#gate_deps:}")" ;;
      gate_cond:*)  gate_cond_val="$(trim "${line#gate_cond:}")" ;;
    esac
  done < "$f"

  [ -z "$status_val" ] && continue

  fname="$(basename "$f" .md)"

  # Count checkboxes in body (after frontmatter)
  body_start=0
  front_count=0
  total_check=0
  unchecked=0
  while IFS= read -r line; do
    if [ "$body_start" -eq 0 ]; then
      [ "$line" = "---" ] && front_count=$((front_count+1))
      [ "$front_count" -ge 2 ] && body_start=1
      continue
    fi
    case "$line" in
      "- [ ] "*|"- [ ]") unchecked=$((unchecked+1)); total_check=$((total_check+1)) ;;
      "- [x] "*|"- [x]"|"- [X] "*|"- [X]") total_check=$((total_check+1)) ;;
    esac
  done < "$f"

  # --- A/B/C (skipped in --gates-only mode) ---
  if [ "$GATES_ONLY" -eq 0 ]; then

    # Type A: done candidate
    case "$status_val" in
      open|wip)
        if [ "$total_check" -ge 1 ] && [ "$unchecked" -eq 0 ]; then
          FIND_FILES+=("$fname")
          FIND_TYPES+=("A")
          FIND_TITLES+=("$title_val")
          log "TYPE-A done-candidate: $fname"
        fi
        ;;
    esac

    # Type B: stale candidate
    case "$status_val" in
      open)
        if [ -n "$updated_val" ]; then
          upd_epoch="$(date_epoch "$updated_val")"
          if [ -n "$upd_epoch" ]; then
            age_sec=$(( NOW - upd_epoch ))
            if [ "$age_sec" -gt "$THRESHOLD_SEC" ]; then
              already=0
              for i in "${!FIND_FILES[@]}"; do
                [ "${FIND_FILES[$i]}" = "$fname" ] && already=1 && break
              done
              if [ "$already" -eq 0 ]; then
                FIND_FILES+=("$fname")
                FIND_TYPES+=("B")
                FIND_TITLES+=("$title_val")
                log "TYPE-B stale-candidate: $fname (updated=$updated_val age=$((age_sec/86400))d)"
              fi
            fi
          fi
        fi
        ;;
    esac

    # Type C: stale wip
    case "$status_val" in
      wip)
        if [ -n "$updated_val" ]; then
          upd_epoch="$(date_epoch "$updated_val")"
          if [ -n "$upd_epoch" ]; then
            age_sec=$(( NOW - upd_epoch ))
            if [ "$age_sec" -gt "$STALE_WIP_SEC" ]; then
              already=0
              for i in "${!FIND_FILES[@]}"; do
                [ "${FIND_FILES[$i]}" = "$fname" ] && already=1 && break
              done
              if [ "$already" -eq 0 ]; then
                FIND_FILES+=("$fname")
                FIND_TYPES+=("C")
                FIND_TITLES+=("$title_val")
                log "TYPE-C stale-wip: $fname (updated=$updated_val age=$((age_sec/86400))d)"
              fi
            fi
          fi
        fi
        ;;
    esac

  fi  # end GATES_ONLY skip

  # -------------------------------------------------------------------------
  # Lint Rule 6: parked ticket MUST have gate_deps OR legacy gate:
  # -------------------------------------------------------------------------
  if [ "$status_val" = "parked" ]; then
    if [ -n "$gate_deps_val" ]; then
      : # new schema present -- passes Rule 6
    elif [ -n "$gate_val" ]; then
      LINT_DEPR_PASS+=("$fname")
      log "LINT-DEPR-PASS: $fname (legacy gate: prose)"
    else
      LINT_R6_VIOLS+=("$fname")
      log "LINT-R6-VIOLATION: $fname (parked with no gate field)"
    fi
  fi

  # -------------------------------------------------------------------------
  # Type D: gate-clearance scan
  # -------------------------------------------------------------------------
  if [ "$status_val" = "parked" ]; then
    if [ -n "$gate_deps_val" ]; then
      # new schema: gate_deps
      ticket_ids="$(echo "$gate_deps_val" | grep -oE "${TICKET_PREFIX}-[0-9]+" || true)"
      dec_ids="$(echo "$gate_deps_val" | grep -oE 'dec-[0-9]+' || true)"

      # Lint-minor: unknown ID references
      for ref_id in $ticket_ids; do
        if ! ticket_known "$ref_id"; then
          LINT_UNKNOWN_IDS+=("${fname}: ${ref_id}")
          log "LINT-MINOR-UNKNOWN: $fname gate_deps references unknown $ref_id"
        fi
      done
      for ref_id in $dec_ids; do
        _dec_check="${DECISIONS_DIR}/dec-${ref_id#dec-}.md"
        if [ ! -f "$_dec_check" ]; then
          LINT_UNKNOWN_IDS+=("${fname}: ${ref_id}")
          log "LINT-MINOR-UNKNOWN: $fname gate_deps references unknown $ref_id"
        fi
      done

      # Lint-minor: circular reference check
      for ref_id in $ticket_ids; do
        if ticket_known "$ref_id" && _check_circular "$ticket_id" "$ref_id" 0; then
          LINT_CIRCULAR+=("${fname} -> ${ref_id}")
          log "LINT-MINOR-CIRCULAR: $fname gate_deps -> $ref_id forms a cycle"
        fi
      done

      if [ -z "$ticket_ids" ] && [ -z "$dec_ids" ]; then
        : # gate_deps has no scannable IDs (prose only)
      else
        all_clear=1
        for ref_id in $ticket_ids; do
          if ! ticket_is_done "$ref_id"; then
            all_clear=0
            log "TYPE-D gate-dep NOT done: $fname -> $ref_id"
            break
          fi
        done
        if [ "$all_clear" -eq 1 ]; then
          for ref_id in $dec_ids; do
            if ! dec_is_done "$ref_id"; then
              all_clear=0
              log "TYPE-D gate-dec NOT decided: $fname -> $ref_id"
              break
            fi
          done
        fi
        if [ "$all_clear" -eq 1 ]; then
          GC_IDS+=("$fname")
          GC_GATES+=("$gate_deps_val")
          GC_PRIORITY+=("${priority_val:--}")
          if [ -n "$gate_cond_val" ]; then
            GC_COND_REMAINING+=("1")
            log "TYPE-D COND-REMAINING: $fname -- deps clear but cond: $gate_cond_val"
          else
            GC_COND_REMAINING+=("0")
            log "TYPE-D UNPARK CANDIDATE: $fname -- gate cleared ($gate_deps_val)"
          fi
        fi
      fi

    elif [ -n "$gate_val" ]; then
      # legacy gate: prose path
      case "$gate_val" in
        owner-review) : ;;  # not scannable
        *)
          ticket_ids="$(echo "$gate_val" | grep -oE "${TICKET_PREFIX}-[0-9]+" || true)"
          dec_ids="$(echo "$gate_val" | grep -oE 'dec-[0-9]+' || true)"

          if [ -z "$ticket_ids" ] && [ -z "$dec_ids" ]; then
            : # no scannable IDs, skip
          else
            all_clear=1
            for ref_id in $ticket_ids; do
              if ! ticket_is_done "$ref_id"; then
                all_clear=0
                log "TYPE-D gate-ref NOT done: $fname -> $ref_id"
                break
              fi
            done
            if [ "$all_clear" -eq 1 ]; then
              for ref_id in $dec_ids; do
                if ! dec_is_done "$ref_id"; then
                  all_clear=0
                  log "TYPE-D gate-dec NOT decided: $fname -> $ref_id"
                  break
                fi
              done
            fi
            if [ "$all_clear" -eq 1 ]; then
              GC_IDS+=("$fname")
              GC_GATES+=("$gate_val")
              GC_PRIORITY+=("${priority_val:--}")
              GC_COND_REMAINING+=("0")
              log "TYPE-D UNPARK CANDIDATE (legacy): $fname -- gate cleared ($gate_val)"
            fi
          fi
          ;;
      esac
    fi
  fi

  # Big-rock: P1 ticket with any status except done
  if [ "$priority_val" = "P1" ] && [ "$status_val" != "done" ]; then
    age_days="-"
    if [ -n "$created_val" ]; then
      cr_epoch="$(date_epoch "$created_val")"
      if [ -n "$cr_epoch" ]; then
        age_days=$(( (NOW - cr_epoch) / 86400 ))
      fi
    fi
    local_gate_disp="${gate_deps_val:-$gate_val}"
    BR_IDS+=("$fname")
    BR_STATUSES+=("$status_val")
    BR_AGES+=("$age_days")
    BR_GATES+=("$local_gate_disp")
    log "BIG-ROCK: $fname (status=$status_val age=${age_days}d)"
  fi

done

TOTAL="${#FIND_FILES[@]}"
GC_TOTAL="${#GC_IDS[@]}"
BR_TOTAL="${#BR_IDS[@]}"
LINT_R6_TOTAL="${#LINT_R6_VIOLS[@]}"
LINT_DEPR_TOTAL="${#LINT_DEPR_PASS[@]}"
LINT_UNKNOWN_TOTAL="${#LINT_UNKNOWN_IDS[@]}"
LINT_CIRCULAR_TOTAL="${#LINT_CIRCULAR[@]}"
log "scan complete: $TOTAL findings (A/B/C), $GC_TOTAL gate-clearance, $BR_TOTAL big-rock P1"
log "lint: Rule-6 violations=$LINT_R6_TOTAL deprecated-pass=$LINT_DEPR_TOTAL unknown-ids=$LINT_UNKNOWN_TOTAL circular=$LINT_CIRCULAR_TOTAL"

# --- build body ---
build_body() {
  local prefix="$1"
  local header="Weekly ticket hygiene sweep"
  [ -n "$prefix" ] && header="${prefix} ${header}"

  local body="${header}"

  # --- findings section (A/B/C) ---
  if [ "$GATES_ONLY" -eq 0 ] && ([ "$TOTAL" -gt 0 ] || [ "$TEST_MODE" -eq 1 ]); then
    if [ "$TOTAL" -eq 0 ]; then
      body="${body}
[hygiene] 0 findings"
    else
      local MAX_LINES=15
      local shown=0
      body="${body}
[hygiene]"
      for i in "${!FIND_FILES[@]}"; do
        if [ "$shown" -ge "$MAX_LINES" ]; then
          remaining=$(( TOTAL - shown ))
          body="${body}
... and ${remaining} more"
          break
        fi
        local fid="${FIND_FILES[$i]}"
        local ftype="${FIND_TYPES[$i]}"
        local ftitle="${FIND_TITLES[$i]}"
        if [ "${#ftitle}" -gt 40 ]; then
          ftitle="${ftitle:0:37}..."
        fi
        if [ "$ftype" = "A" ]; then
          body="${body}
${fid} -- done candidate (all checkboxes checked)"
        elif [ "$ftype" = "C" ]; then
          body="${body}
${fid} -- stale wip (3+ days)"
        else
          body="${body}
${fid} -- no update in 14+ days"
        fi
        shown=$((shown+1))
      done
    fi
  fi

  # --- gate-clearance section (D) ---
  if [ "$GC_TOTAL" -gt 0 ]; then
    body="${body}
[unpark candidates]"
    for i in "${!GC_IDS[@]}"; do
      local gc_id="${GC_IDS[$i]}"
      local gc_gate="${GC_GATES[$i]}"
      local gc_cond="${GC_COND_REMAINING[$i]}"
      if [ "${#gc_gate}" -gt 60 ]; then
        gc_gate="${gc_gate:0:57}..."
      fi
      if [ "$gc_cond" = "1" ]; then
        body="${body}
COND-REMAINING: ${gc_id} -- deps clear, condition remains (${gc_gate})"
      else
        body="${body}
UNPARK CANDIDATE: ${gc_id} -- gate cleared (${gc_gate})"
      fi
    done
  fi

  # --- lint section ---
  local lint_total=$(( LINT_R6_TOTAL + LINT_UNKNOWN_TOTAL + LINT_CIRCULAR_TOTAL ))
  if [ "$lint_total" -gt 0 ]; then
    body="${body}
[gate lint]"
    if [ "$LINT_R6_TOTAL" -gt 0 ]; then
      body="${body}
Rule-6 violations: ${LINT_R6_TOTAL} (parked with no gate field):"
      for v in "${LINT_R6_VIOLS[@]}"; do
        body="${body}
  ${v}"
      done
    fi
    if [ "$LINT_DEPR_TOTAL" -gt 0 ]; then
      body="${body}
deprecated-pass: ${LINT_DEPR_TOTAL} (legacy gate: prose -- migrate to gate_deps)"
    fi
    if [ "$LINT_UNKNOWN_TOTAL" -gt 0 ]; then
      body="${body}
unknown IDs: ${LINT_UNKNOWN_TOTAL}:"
      for v in "${LINT_UNKNOWN_IDS[@]}"; do
        body="${body}
  ${v}"
      done
    fi
    if [ "$LINT_CIRCULAR_TOTAL" -gt 0 ]; then
      body="${body}
circular refs: ${LINT_CIRCULAR_TOTAL}:"
      for v in "${LINT_CIRCULAR[@]}"; do
        body="${body}
  ${v}"
      done
    fi
  fi

  # --- big-rock section (always included when non-empty) ---
  if [ "$BR_TOTAL" -gt 0 ]; then
    body="${body}
[big-rock P1]"
    for i in "${!BR_IDS[@]}"; do
      local br_id="${BR_IDS[$i]}"
      local br_st="${BR_STATUSES[$i]}"
      local br_age="${BR_AGES[$i]}"
      local br_gate="${BR_GATES[$i]}"
      local gate_disp="-"
      if [ -n "$br_gate" ]; then
        gate_disp="$br_gate"
        if [ "${#gate_disp}" -gt 35 ]; then
          gate_disp="${gate_disp:0:32}..."
        fi
      fi
      body="${body}
${br_id}  ${br_st}  ${br_age}d  ${gate_disp}"
    done
  fi

  echo "$body"
}

# ---------------------------------------------------------------------------
# _UNPARK ledger
#
# The ledger (worklog/_UNPARK.md) is the PERSISTENT signal for unpark
# candidates. Single-writer invariant: this scan is the SOLE writer. Every
# run regenerates the WHOLE file idempotently from the tickets' real state.
# Dispose of a candidate by manipulating the ticket surface (parked -> open);
# the next scan reflects that automatically. Never hand-edit this ledger.
#
# Freshness: the header carries a last-scan timestamp so a dead scan shows
# up as a stale stamp.
#
# Format: one candidate per line --
#   <ticket-id>  <priority>  <UNPARK|COND>  gate: <clearance-basis>
# ---------------------------------------------------------------------------
write_unpark_ledger() {
  local stamp
  stamp="$(date '+%Y-%m-%d %H:%M:%S %Z')"
  local tmp="${UNPARK_LEDGER}.tmp.$$"

  {
    echo "# _UNPARK -- unpark candidate ledger (auto-generated by ticket-hygiene.sh)"
    echo "#"
    echo "# This file is written solely by ticket-hygiene.sh on every run."
    echo "# Full regeneration from ticket real state (idempotent) -- do not hand-edit."
    echo "# To dispose of a candidate, change the ticket surface (parked -> open);"
    echo "# the next scan will reflect it automatically."
    echo "#"
    echo "# Format: <ticket-id>  <priority>  <UNPARK|COND>  gate: <clearance basis>"
    echo "# UNPARK = gate_deps all resolved, no gate_cond (ready to promote)."
    echo "# COND   = deps resolved but gate_cond remains (verify condition first)."
    echo "#"
    echo "last-scan: ${stamp}"
    echo "candidates: ${GC_TOTAL}"
    echo ""
    if [ "$GC_TOTAL" -eq 0 ]; then
      echo "(no candidates)"
    else
      local i
      for i in "${!GC_IDS[@]}"; do
        local gc_id="${GC_IDS[$i]}"
        local gc_gate="${GC_GATES[$i]}"
        local gc_cond="${GC_COND_REMAINING[$i]}"
        local gc_pri="${GC_PRIORITY[$i]}"
        local kind="UNPARK"
        [ "$gc_cond" = "1" ] && kind="COND"
        echo "${gc_id}  ${gc_pri}  ${kind}  gate: ${gc_gate}"
      done
    fi
  } > "$tmp"

  mv "$tmp" "$UNPARK_LEDGER"
  log "unpark ledger regenerated: $UNPARK_LEDGER ($GC_TOTAL candidates, last-scan=$stamp)"
}

# --- always regenerate the persistent unpark ledger (idempotent) ---
# In dry-run we skip the on-disk write (report only).
if [ "$DRY_RUN" -eq 1 ]; then
  log "dry-run: skipping unpark ledger write"
else
  write_unpark_ledger
fi

# --- gates-only mode: ledger regeneration IS the output; no push ---
if [ "$GATES_ONLY" -eq 1 ]; then
  log "gates-only: ledger regenerated, no push"
  exit 0
fi

# --- weekly push path ---
SHOULD_PUSH=0
[ "$TOTAL" -gt 0 ] && SHOULD_PUSH=1
[ "$GC_TOTAL" -gt 0 ] && SHOULD_PUSH=1
[ "$BR_TOTAL" -gt 0 ] && SHOULD_PUSH=1
[ "$LINT_R6_TOTAL" -gt 0 ] && SHOULD_PUSH=1
[ "$TEST_MODE" -eq 1 ] && SHOULD_PUSH=1

if [ "$SHOULD_PUSH" -eq 1 ]; then
  if [ "$TEST_MODE" -eq 1 ]; then
    BODY="$(build_body "[TEST]")"
  else
    BODY="$(build_body "")"
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "=== DRY-RUN: would-be push body ==="
    echo "$BODY"
    echo "=== end ==="
    log "dry-run complete (no push)"
  elif [ -n "$PUSH_CMD" ]; then
    log "pushing via PUSH_CMD..."
    bash "$PUSH_CMD" --text "$BODY"
    log "push complete"
  else
    log "PUSH_CMD not set -- printing body to stdout (no push)"
    echo "$BODY"
  fi
else
  log "no findings -- silent exit"
fi

exit 0
