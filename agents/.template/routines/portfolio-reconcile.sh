#!/usr/bin/env bash
# portfolio-reconcile.sh -- WEEKLY portfolio reconcile pass, v1 skeleton.
# Framework asset; spec of record: docs/PORTFOLIO-CORE.md (D-15 RECONCILE
# duties, generic subset). Registered per instance by the
# dogany-portfolio-setup skill the moment any declared-state column exists
# (C.3 escalation trigger) -- the framework never pre-registers this cron.
#
# v1 scope (deliberately minimal -- the duties that are generic):
#   1. Structural/schema lint: runs portfolio-core-lint.py on the index.
#   2. E4 staleness: `generated` stamp vs threshold (default >30d WARN).
#   3. E1 multi-source existence diff over a SMALL supported source-type set:
#      dir-glob, file-glob (marker/config files), launchd-prefix (label list).
#      Identifier present in a source but absent from index rows AND exclusion
#      list = FAIL (no silent enrollment).
#   4. E5 disappearance REPORT: row location missing on disk = FAIL, routed to
#      retirement. Report-only -- the tombstone COMMIT stays with the index's
#      designated writer; this script never edits the index.
#   5. Exclusion list printed in full (E9 item 6 -- visibility is the check on
#      the list's own autonomy).
# NOT in v1 (instance-extension duties, documented in the PORTFOLIO-CORE
# playbooks; implement locally where the estate needs them): E3 declared-state
# vs ground-truth diffs (git/PR state), E8 misclassification detector,
# E2 grandfather burn-down surfacing.
#
# Checker liveness (E4): this pass prints its own last-run line; the instance
# wires that line onto the surface declared by the index's liveness-terminal
# header item (console badge, weekly report push, ...).
#
# Usage:
#   bash routines/portfolio-reconcile.sh [--index PATH] [--config PATH]
#                                        [--stale-days N] [--enum-config FILE]
#   defaults: index  = <agent-root>/product/PORTFOLIO.md
#             config = <agent-root>/config/portfolio-reconcile.conf
#             stale-days = 30
# Config format (one directive per line; '#' comments; blank lines ignored):
#   dir-glob <glob>          each matching DIRECTORY yields identifier =
#                            lowercased basename
#   file-glob <glob>         each matching FILE yields identifier = lowercased
#                            basename of its parent dir (dot-dirs like .prdt
#                            are skipped upward one level -- marker-file
#                            convention <unit>/.marker/config)
#   launchd-prefix <prefix>  labels from `launchctl list` starting with prefix;
#                            identifier = lowercased label minus prefix
#   map <raw> <row-id>       optional alias: enumerated identifier <raw> counts
#                            as index row <row-id>
# No config file -> the E1 diff is SKIPPED with a visible note (declare your
# enumeration sources to activate it; the index header's enum-sources item
# names them, this config makes them machine-runnable).
# Exit: 0 = clean (warnings allowed), 1 = lint FAIL or E1/E5 findings.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LINT="$SCRIPT_DIR/lib/portfolio-core-lint.py"

INDEX="$AGENT_DIR/product/PORTFOLIO.md"
CONFIG="$AGENT_DIR/config/portfolio-reconcile.conf"
STALE_DAYS=30
ENUM_CONFIG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --index)       INDEX="$2"; shift 2 ;;
    --config)      CONFIG="$2"; shift 2 ;;
    --stale-days)  STALE_DAYS="$2"; shift 2 ;;
    --enum-config) ENUM_CONFIG="$2"; shift 2 ;;
    -h|--help)     sed -n '2,55p' "$0"; exit 0 ;;
    *) echo "[portfolio-reconcile] unknown option: $1" >&2; exit 1 ;;
  esac
done

FAIL=0
WARN=0
echo "[portfolio-reconcile] index = $INDEX"

if [ ! -f "$INDEX" ]; then
  echo "[portfolio-reconcile] no index at $INDEX -- nothing to reconcile (exit 0)"
  echo "[portfolio-reconcile] last-run: $(date +%Y-%m-%dT%H:%M) verdict=NO-INDEX"
  exit 0
fi

# ---------------------------------------------------------------------------
# 1) Structural/schema lint (parse-or-die; exclusion list printed in full by
#    the lint's EXCLUSION findings on the core:1 path).
# ---------------------------------------------------------------------------
echo "[portfolio-reconcile] -- lint --"
if [ -n "$ENUM_CONFIG" ]; then
  LINT_OUT="$(python3 "$LINT" --enum-config "$ENUM_CONFIG" "$INDEX" reconcile 2>&1)" || FAIL=1
else
  LINT_OUT="$(python3 "$LINT" "$INDEX" reconcile 2>&1)" || FAIL=1
fi
printf '%s\n' "$LINT_OUT"
if [ "$FAIL" = "1" ]; then
  echo "[portfolio-reconcile] LINT FAIL -- stopping (parse-or-die; fix the index first)"
  echo "[portfolio-reconcile] last-run: $(date +%Y-%m-%dT%H:%M) verdict=LINT-FAIL"
  exit 1
fi

# ---------------------------------------------------------------------------
# 2) E4 staleness: generated stamp vs threshold. Absent stamp already WARNs in
#    the lint (D-14); here we add the age check the lint does not do.
# ---------------------------------------------------------------------------
echo "[portfolio-reconcile] -- staleness (E4) --"
GENERATED="$(sed -n 's/^#*[[:space:]]*generated:[[:space:]]*//p' "$INDEX" | head -1)"
if [ -n "$GENERATED" ]; then
  GEN_EPOCH="$(python3 - "$GENERATED" <<'PYEOF'
import sys, datetime
v = sys.argv[1].strip()
for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
    try:
        print(int(datetime.datetime.strptime(v, fmt).timestamp())); break
    except ValueError:
        pass
PYEOF
)"
  if [ -n "$GEN_EPOCH" ]; then
    AGE_DAYS=$(( ( $(date +%s) - GEN_EPOCH ) / 86400 ))
    if [ "$AGE_DAYS" -gt "$STALE_DAYS" ]; then
      echo "  WARN: generated stamp is ${AGE_DAYS}d old (threshold ${STALE_DAYS}d) -- stale index (E4)"
      WARN=1
    else
      echo "  OK: generated stamp ${AGE_DAYS}d old (threshold ${STALE_DAYS}d)"
    fi
  else
    echo "  NOTE: generated stamp not age-comparable: '$GENERATED' (lint governs its grammar)"
  fi
else
  echo "  NOTE: no generated stamp (lint already WARNs per D-14; no age check possible)"
fi

# ---------------------------------------------------------------------------
# Row + exclusion extraction (via the lint's structural dumps -- one parser,
# one home; both ran through the same parse-or-die above).
# ---------------------------------------------------------------------------
ROWS_TSV="$(python3 "$LINT" --dump-rows "$INDEX")" || { echo "[portfolio-reconcile] dump-rows failed"; exit 1; }
EXCLUSIONS="$(python3 "$LINT" --dump-exclusions "$INDEX")" || true

echo "[portfolio-reconcile] -- exclusion list (full print, E9 item 6) --"
if [ -n "$EXCLUSIONS" ]; then
  printf '%s\n' "$EXCLUSIONS" | sed 's/^/  EXCLUDED: /'
else
  echo "  (empty -- legal)"
fi

_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

ROW_IDS=""
while IFS=$'\t' read -r rid _loc _state _last; do
  [ -n "$rid" ] && [ "$rid" != "-" ] && ROW_IDS="$ROW_IDS $(_lower "$rid")"
done <<< "$ROWS_TSV"

EXCL_IDS=""
while IFS= read -r item; do
  [ -n "$item" ] && EXCL_IDS="$EXCL_IDS $(_lower "$item")"
done <<< "$EXCLUSIONS"

_known() {
  # _known <identifier> -> 0 when identifier is an index row or excluded
  local id; id="$(_lower "$1")"
  case " $ROW_IDS " in *" $id "*) return 0 ;; esac
  case " $EXCL_IDS " in *" $id "*) return 0 ;; esac
  return 1
}

# ---------------------------------------------------------------------------
# 3) E1 multi-source existence diff (dir-glob / file-glob / launchd-prefix).
# ---------------------------------------------------------------------------
echo "[portfolio-reconcile] -- existence diff (E1) --"
if [ -f "$CONFIG" ]; then
  # Aliases first (map <raw> <row-id>).
  declare -a MAP_RAW=() MAP_ID=()
  while read -r kind a b; do
    case "$kind" in
      map) MAP_RAW+=("$(_lower "$a")"); MAP_ID+=("$(_lower "$b")") ;;
    esac
  done < <(sed -e 's/#.*$//' "$CONFIG")

  _alias() {
    # _alias <raw> -> mapped id (or raw unchanged)
    local raw i; raw="$(_lower "$1")"
    for i in "${!MAP_RAW[@]}"; do
      if [ "${MAP_RAW[$i]}" = "$raw" ]; then printf '%s' "${MAP_ID[$i]}"; return; fi
    done
    printf '%s' "$raw"
  }

  SOURCES=0
  while read -r kind arg _rest; do
    [ -n "$kind" ] || continue
    case "$kind" in
      dir-glob)
        SOURCES=$((SOURCES + 1))
        for d in $(eval echo "$arg"); do
          [ -d "$d" ] || continue
          ident="$(_alias "$(basename "$d")")"
          if ! _known "$ident"; then
            echo "  E1 FAIL: unenrolled identifier '$ident' (dir-glob $arg) -- not in rows, not excluded"
            FAIL=1
          fi
        done
        ;;
      file-glob)
        SOURCES=$((SOURCES + 1))
        for f in $(eval echo "$arg"); do
          [ -f "$f" ] || continue
          parent="$(dirname "$f")"
          base="$(basename "$parent")"
          # Marker-file convention: skip one dot-dir level (<unit>/.marker/file).
          case "$base" in .*) base="$(basename "$(dirname "$parent")")" ;; esac
          ident="$(_alias "$base")"
          if ! _known "$ident"; then
            echo "  E1 FAIL: unenrolled identifier '$ident' (file-glob $arg) -- not in rows, not excluded"
            FAIL=1
          fi
        done
        ;;
      launchd-prefix)
        SOURCES=$((SOURCES + 1))
        if command -v launchctl >/dev/null 2>&1; then
          while IFS= read -r label; do
            [ -n "$label" ] || continue
            ident="$(_alias "${label#"$arg"}")"
            if ! _known "$ident"; then
              echo "  E1 FAIL: unenrolled identifier '$ident' (launchd-prefix $arg) -- not in rows, not excluded"
              FAIL=1
            fi
          done < <(launchctl list 2>/dev/null | awk '{print $3}' | grep -F "$arg" || true)
        else
          echo "  NOTE: launchctl unavailable -- launchd-prefix source '$arg' skipped"
        fi
        ;;
      map) : ;;  # handled above
      *) echo "  WARN: unknown source type '$kind' in $CONFIG (supported: dir-glob, file-glob, launchd-prefix, map)"; WARN=1 ;;
    esac
  done < <(sed -e 's/#.*$//' "$CONFIG")
  if [ "$SOURCES" -lt 2 ]; then
    echo "  NOTE: only $SOURCES enumeration source(s) configured -- E1 wants >= 2 independent sources (or a declared accepted-gap)"
  fi
else
  echo "  SKIPPED: no reconcile config at $CONFIG -- declare enumeration sources to activate the E1 diff"
fi

# ---------------------------------------------------------------------------
# 4) E5 disappearance report: local-path row locations missing on disk.
#    Report-only; tombstone commit stays with the designated writer.
# ---------------------------------------------------------------------------
echo "[portfolio-reconcile] -- disappearance report (E5) --"
while IFS=$'\t' read -r rid loc state _last; do
  [ -n "$rid" ] && [ "$rid" != "-" ] || continue
  case "$state" in retired|frozen) continue ;; esac   # already recorded transitions
  case "$loc" in
    /*|\~*)
      loc_exp="${loc/#\~/$HOME}"
      if [ ! -e "$loc_exp" ]; then
        echo "  E5 DISAPPEARANCE: row '$rid' location missing on disk: $loc"
        echo "    -> route to retirement (state token or TOMBSTONE entry); COMMIT is the designated writer's, not this script's"
        FAIL=1
      fi
      ;;
    *) : ;;  # '-' or URL-shaped locations: not locally checkable
  esac
done <<< "$ROWS_TSV"

# ---------------------------------------------------------------------------
# Verdict + checker-liveness line (E4).
# ---------------------------------------------------------------------------
if [ "$FAIL" = "1" ]; then VERDICT="FAIL"; elif [ "$WARN" = "1" ]; then VERDICT="WARN"; else VERDICT="OK"; fi
echo "[portfolio-reconcile] last-run: $(date +%Y-%m-%dT%H:%M) verdict=$VERDICT"
[ "$FAIL" = "1" ] && exit 1
exit 0
