#!/usr/bin/env bash
# routines/ledger-reconcile.sh -- DGN-1730
#
# PURPOSE: flip product/auto-loop-ledger.md rows that are still `pending-review`
# although their work is already in base. A dsp/* branch merged by hand never
# flips its row (only loop-merge.sh does); the read side (status-footer.py
# _filter_review_rows) already ignores such rows, this is the write side that
# makes the ledger itself true again.
#
# A pending-review row is "merged" when its branch tip -- or, if the branch
# ref is gone, the HEAD=<sha> in its note -- is an ancestor of base.
# Repos checked: the instance repo (DOGANY_INSTANCE_ROOT, default = this
# instance root, base main) and the canonical repo (DOGANY_REPO_ROOT, else
# .instance.conf, base dev). Base falls back to origin/HEAD, then main/master.
# Merged in ANY readable repo -> flip. Flips go through lib/ledger-update.sh
# (the ONLY ledger write path) with note "reconciled: already in base".
#
# USAGE: ledger-reconcile.sh [--dry-run] [--ledger PATH]
# OUTPUT: one line per flipped row, then
#         "ledger-reconcile: flipped=N unmerged=N unresolvable=N [dry-run]"
# EXIT: 0 unless a ledger write failed (or bad usage). A missing ledger, or no
#       readable git repo, is a reported no-op (exit 0): housekeeping must
#       never fail its host routine.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LEDGER="${DOGANY_LEDGER_FILE:-${ROOT}/product/auto-loop-ledger.md}"
UPDATE="${SCRIPT_DIR}/lib/ledger-update.sh"
NOTE="reconciled: already in base"
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --ledger)  LEDGER="${2:?--ledger needs a path}"; shift 2 ;;
    *) echo "USAGE: ledger-reconcile.sh [--dry-run] [--ledger PATH]" >&2; exit 1 ;;
  esac
done

log() { echo "ledger-reconcile: $*"; }
tag=""; [[ $DRY_RUN -eq 1 ]] && tag=" dry-run"

if [[ ! -f "$LEDGER" ]]; then
  log "no ledger at ${LEDGER} -- nothing to do"
  exit 0
fi

# ---- repos: "path<TAB>preferred-base" -------------------------------------
INSTANCE_ROOT="${DOGANY_INSTANCE_ROOT:-$ROOT}"
CANON="${DOGANY_REPO_ROOT:-}"
if [[ -z "$CANON" && -f "${ROOT}/.instance.conf" ]]; then
  CANON="$(sed -n 's/^DOGANY_REPO_ROOT=//p' "${ROOT}/.instance.conf" | head -1 | tr -d "\"'")"
fi
REPOS=("${INSTANCE_ROOT}"$'\t'main)
if [[ -n "$CANON" && "$(cd "$CANON" 2>/dev/null && pwd -P)" != "$(cd "$INSTANCE_ROOT" 2>/dev/null && pwd -P)" ]]; then
  REPOS+=("${CANON}"$'\t'dev)
fi

# resolve_base <repo> <preferred> -> prints a commit-ish, or nothing
resolve_base() {
  local repo="$1" pref="$2" b
  for b in "refs/heads/${pref}" "refs/remotes/origin/HEAD" refs/heads/main refs/heads/master; do
    if git -C "$repo" rev-parse --verify -q "${b}^{commit}" >/dev/null 2>&1; then
      echo "$b"; return 0
    fi
  done
  return 1
}

# repo_state <repo> <base> <branch> <note> -> merged | unmerged | none
repo_state() {
  local repo="$1" base="$2" branch="$3" note="$4" sha="" r
  for r in "refs/heads/${branch}" "refs/remotes/origin/${branch}"; do
    sha="$(git -C "$repo" rev-parse --verify -q "${r}^{commit}" 2>/dev/null)" && break
    sha=""
  done
  if [[ -z "$sha" ]]; then
    local h
    h="$(printf '%s' "$note" | sed -n 's/.*HEAD=\([0-9a-fA-F]\{7,40\}\).*/\1/p' | head -1)"
    [[ -n "$h" ]] && sha="$(git -C "$repo" rev-parse --verify -q "${h}^{commit}" 2>/dev/null)"
  fi
  if [[ -z "$sha" ]]; then echo none; return; fi
  if git -C "$repo" merge-base --is-ancestor "$sha" "$base" 2>/dev/null; then
    echo merged
  else
    echo unmerged
  fi
}

# Only repos that are readable git checkouts with a resolvable base count.
R_PATHS=(); R_BASES=()
for entry in "${REPOS[@]}"; do
  path="${entry%%$'\t'*}"; pref="${entry#*$'\t'}"
  [[ -d "$path" ]] || continue
  base="$(resolve_base "$path" "$pref")" || continue
  R_PATHS+=("$path"); R_BASES+=("$base")
done
if [[ ${#R_PATHS[@]} -eq 0 ]]; then
  log "no readable git repo with a base branch -- nothing to do"
  exit 0
fi

flipped=0; unmerged=0; unresolvable=0; failed=0

# rows: branch<TAB>note for every pending-review row
ROWS="$(awk -F'|' '
  function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }
  /^\|/ && trim($4) == "pending-review" { print trim($2) "\t" trim($9) }
' "$LEDGER")"

while IFS=$'\t' read -r branch note; do
  [[ -z "${branch:-}" ]] && continue
  merged=0; seen=0
  i=0
  while [[ $i -lt ${#R_PATHS[@]} ]]; do
    st="$(repo_state "${R_PATHS[$i]}" "${R_BASES[$i]}" "$branch" "${note:-}")"
    case "$st" in
      merged)   merged=1; seen=1 ;;
      unmerged) seen=1 ;;
    esac
    [[ $merged -eq 1 ]] && break
    i=$((i + 1))
  done
  if [[ $merged -eq 1 ]]; then
    if [[ $DRY_RUN -eq 1 ]]; then
      log "would flip ${branch} -> merged"
      flipped=$((flipped + 1))
    elif bash "$UPDATE" "$branch" merged "$NOTE" --ledger "$LEDGER" >/dev/null; then
      log "flipped ${branch} -> merged"
      flipped=$((flipped + 1))
    else
      log "FAILED to flip ${branch}" >&2
      failed=$((failed + 1))
    fi
  elif [[ $seen -eq 1 ]]; then
    unmerged=$((unmerged + 1))
  else
    log "unresolvable ${branch} (no branch ref, no HEAD sha in note)"
    unresolvable=$((unresolvable + 1))
  fi
done <<< "$ROWS"

log "flipped=${flipped} unmerged=${unmerged} unresolvable=${unresolvable}${tag}"
[[ $failed -eq 0 ]]
