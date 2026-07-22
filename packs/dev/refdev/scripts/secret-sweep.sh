#!/bin/bash
# secret-sweep.sh -- pre-push SAFETY GATE (dev pack, generalized).
#
# Scans a target repo's TRACKED files for secrets / owner identity /
# forbidden files that must never enter a shared git history.
# Exit 0 = clean, 1 = hit(s), 2 = usage error.
#
# Usage:
#   secret-sweep.sh [<repo-dir>]                    scan tracked files (git ls-files)
#   secret-sweep.sh --staged [<repo-dir>]           scan staged files only
#   secret-sweep.sh --instance-root <path> [...]    override the instance root
#   secret-sweep.sh -h | --help
#
# Owner identity patterns (per-instance config, NOT shipped by the pack):
#   <instance-root>/config/secret-patterns.conf
#   One pattern per line:  <label>|<extended-regex>
#   Blank lines and #-comments ignored. Example:
#     owner-email|(alice|alice\.work)@example\.com
#     owner-name|Alice Example
#   The same patterns are also matched against commit author/committer
#   metadata (git history is a separate leak surface from file contents).
#   When the config is MISSING or EMPTY the sweep runs STRUCTURAL scans
#   only and prints an explicit warning -- the pass message always states
#   what was actually scanned (no placebo pass).
#   <instance-root> defaults to the parent of this script's directory
#   (the script installs at <instance-root>/scripts/).
#
# Structural scans (always on):
#   S1 telegram-token          [0-9]{8,10}:[A-Za-z0-9_-]{35}
#   S2 generic-key             sk-... / ghp_... / AKIA...
#   S3 env-secret-line         (TOKEN|SECRET|API_KEY|PASSWORD)=<8+ chars>
#   S4 forbidden-tracked-file  .env / *.db / sessions.json (must never track)
#
# Allowlist: a `.sweepignore` file at the scanned repo's root lists path
# globs whose hits are legit placeholders (e.g. .env.example). One glob per
# line; blank lines and #-comments ignored. Matched paths are skipped.

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_INSTANCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---- args -------------------------------------------------------------------
MODE="tracked"
REPO=""
INSTANCE_ROOT="$DEFAULT_INSTANCE_ROOT"
EXPECT_IROOT=0
for a in "$@"; do
  if [ "$EXPECT_IROOT" = "1" ]; then
    INSTANCE_ROOT="$a"; EXPECT_IROOT=0; continue
  fi
  case "$a" in
    --staged) MODE="staged" ;;
    --instance-root) EXPECT_IROOT=1 ;;
    -h|--help)
      sed -n '2,37p' "$0"; exit 0 ;;
    -*)
      echo "unknown flag: $a" >&2; exit 2 ;;
    *)
      REPO="$a" ;;
  esac
done
if [ "$EXPECT_IROOT" = "1" ]; then
  echo "ERROR: --instance-root requires a path argument" >&2; exit 2
fi
REPO="${REPO:-$(pwd)}"

if ! git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: not a git repo: $REPO" >&2
  exit 2
fi
REPO_ROOT="$(git -C "$REPO" rev-parse --show-toplevel)"

# ---- structural patterns (always on) ------------------------------------------
declare -a CAT_NAME CAT_PAT
CAT_NAME[1]="telegram-token";  CAT_PAT[1]='[0-9]{8,10}:[A-Za-z0-9_-]{35}'
CAT_NAME[2]="generic-key";     CAT_PAT[2]='sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}'
CAT_NAME[3]="env-secret-line"; CAT_PAT[3]='(TOKEN|SECRET|API_KEY|PASSWORD)=[^[:space:]]{8,}'
CAT_NAME[4]="forbidden-tracked-file"   # filename-based, no content pattern
STRUCT_MAX=4

# ---- owner identity patterns (per-instance config) -> categories 5..N ---------
PATTERNS_CONF="$INSTANCE_ROOT/config/secret-patterns.conf"
CAT_MAX=$STRUCT_MAX
ID_COUNT=0
if [ -f "$PATTERNS_CONF" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -n "$line" ] || continue
    case "$line" in
      *"|"*) lbl="${line%%|*}"; pat="${line#*|}" ;;
      *)     lbl="owner-pattern"; pat="$line" ;;
    esac
    [ -n "$pat" ] || continue
    CAT_MAX=$(( CAT_MAX + 1 ))
    CAT_NAME[$CAT_MAX]="$lbl"
    CAT_PAT[$CAT_MAX]="$pat"
    ID_COUNT=$(( ID_COUNT + 1 ))
  done < "$PATTERNS_CONF"
fi
COMMIT_CAT=$(( CAT_MAX + 1 ))
CAT_NAME[$COMMIT_CAT]="commit-identity"

# ---- file list (bash 3.2: newline-delimited, no mapfile) ----------------------
if [ "$MODE" = "staged" ]; then
  # staged, added/copied/modified only (skip deletions)
  FILE_LIST="$(git -C "$REPO_ROOT" diff --cached --name-only --diff-filter=ACM)"
else
  FILE_LIST="$(git -C "$REPO_ROOT" ls-files)"
fi
FILE_COUNT=0
[ -n "$FILE_LIST" ] && FILE_COUNT="$(printf '%s\n' "$FILE_LIST" | grep -c .)"

# ---- allowlist ----------------------------------------------------------------
ALLOW=""
if [ -f "$REPO_ROOT/.sweepignore" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"                       # strip comment
    line="$(echo "$line" | tr -d '[:space:]')"
    [ -n "$line" ] && ALLOW="${ALLOW}${line}"$'\n'
  done < "$REPO_ROOT/.sweepignore"
fi

is_allowed() {  # $1 = repo-relative path
  local p="$1" g
  [ -z "$ALLOW" ] && return 1
  while IFS= read -r g; do
    [ -z "$g" ] && continue
    # shellcheck disable=SC2053
    case "$p" in $g) return 0 ;; esac
  done <<EOF
$ALLOW
EOF
  return 1
}

# ---- scan ----------------------------------------------------------------------
TOTAL=0
HITLINES=""
declare -a CAT_COUNT
j=1
while [ "$j" -le "$COMMIT_CAT" ]; do CAT_COUNT[$j]=0; j=$(( j + 1 )); done

record() {  # $1=cat idx  $2=file  $3=detail
  CAT_COUNT[$1]=$(( ${CAT_COUNT[$1]:-0} + 1 ))
  TOTAL=$(( TOTAL + 1 ))
  HITLINES="${HITLINES}  [cat${1} ${CAT_NAME[$1]}] ${2}${3:+ :: }${3}"$'\n'
}

while IFS= read -r f; do
  [ -z "$f" ] && continue
  if is_allowed "$f"; then continue; fi
  # Skip the patterns config file itself (false positive by construction)
  [ "$f" = "config/secret-patterns.conf" ] && continue
  abs="$REPO_ROOT/$f"

  # S4: forbidden filenames (tracked at all = violation)
  base="$(basename "$f")"
  case "$base" in
    .env|sessions.json) record 4 "$f" "" ;;
  esac
  case "$f" in
    *.db|*.db-wal|*.db-shm) record 4 "$f" "" ;;
  esac

  # content scans only for existing regular files (skip binaries/gone)
  [ -f "$abs" ] || continue
  if grep -Iq . "$abs" 2>/dev/null; then :; else continue; fi   # skip binary

  i=1
  while [ "$i" -le "$CAT_MAX" ]; do
    if [ "$i" = "4" ]; then i=$(( i + 1 )); continue; fi   # filename-based
    m="$(grep -nE "${CAT_PAT[$i]}" "$abs" 2>/dev/null | head -3)"
    if [ -n "$m" ] && [ "$i" = "3" ]; then
      # S3 is the noisiest: filter obvious NON-secrets (placeholders / code).
      # ["'] after = is an optional opening quote; tolerate it before the value.
      m="$(printf '%s\n' "$m" \
        | grep -Ev '=["'"'"']?(your_|changeme|change_me|xxx|placeholder|example|dummy|test|<)' \
        | grep -Ev '=["'"'"']?(\$\(|\$\{|\$[A-Za-z_])' \
        | grep -Ev '=["'"'"']["'"'"'] *$' )"
    fi
    if [ -n "$m" ]; then
      # collapse to first matching line number for the summary
      firstln="$(echo "$m" | head -1 | cut -d: -f1)"
      record "$i" "$f" "line ${firstln}"
    fi
    i=$(( i + 1 ))
  done
done <<EOF
$FILE_LIST
EOF

# ---- commit metadata identity (author/committer of EVERY commit) --------------
# File contents are covered above; git history metadata is a separate leak
# surface. Runs only when owner patterns are configured (same pattern set).
if [ "$ID_COUNT" -gt 0 ]; then
  IDENT_PAT=""
  i=$(( STRUCT_MAX + 1 ))
  while [ "$i" -le "$CAT_MAX" ]; do
    if [ -z "$IDENT_PAT" ]; then IDENT_PAT="${CAT_PAT[$i]}"; else IDENT_PAT="${IDENT_PAT}|${CAT_PAT[$i]}"; fi
    i=$(( i + 1 ))
  done
  IDENT_HITS="$(git -C "$REPO_ROOT" log --all --format='%h %an <%ae> / %cn <%ce>' 2>/dev/null \
    | grep -E "$IDENT_PAT" | head -5)"
  if [ -n "$IDENT_HITS" ]; then
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      record "$COMMIT_CAT" "(commit)" "$line"
    done <<EOF
$IDENT_HITS
EOF
  fi
fi

# ---- report --------------------------------------------------------------------
echo "secret-sweep :: repo=$REPO_ROOT mode=$MODE files=$FILE_COUNT"
if [ "$ID_COUNT" -eq 0 ]; then
  echo "WARNING: owner patterns not configured -- identity sweep skipped"
  echo "  (expected at $PATTERNS_CONF; structural scans only)"
  SCOPE="structural scans only (telegram-token, generic-key, env-secret-line, forbidden-tracked-file) -- owner identity NOT swept"
else
  SCOPE="structural scans (telegram-token, generic-key, env-secret-line, forbidden-tracked-file) + owner-identity ($ID_COUNT configured pattern(s)) + commit-identity metadata"
fi

if [ "$TOTAL" -eq 0 ]; then
  echo "RESULT: CLEAN (0 hits; scanned: $SCOPE)"
  exit 0
fi

echo "RESULT: DIRTY ($TOTAL hit(s); scanned: $SCOPE)"
echo "-- category summary --"
i=1
while [ "$i" -le "$COMMIT_CAT" ]; do
  c="${CAT_COUNT[$i]:-0}"
  [ "$c" -gt 0 ] && printf "  cat%s %-24s %d\n" "$i" "${CAT_NAME[$i]}" "$c"
  i=$(( i + 1 ))
done
echo "-- hits --"
printf "%s" "$HITLINES"
echo "STOP: do not push until clean (or allowlist legit placeholders in .sweepignore)."
exit 1
