#!/bin/bash
# secret-sweep.sh -- pre-public-push SAFETY GATE for Dogany repos (DGN-076).
#
# Scans a target repo's TRACKED files for personal data / secrets / forbidden
# files that must never enter a public git history. Exit 0 = clean, 1 = hit(s).
#
# Usage:
#   secret-sweep.sh [<repo-dir>]                      scan tracked files (git ls-files)
#   secret-sweep.sh --staged [<repo>]                 scan staged files only
#   secret-sweep.sh --outbound-diff <base-ref> [<repo>]  D7 tripwire: scan only ADDED
#                                                       lines of git diff <base-ref>..HEAD
#   secret-sweep.sh -h | --help
#
# Allowlist: a `.sweepignore` file at the repo root lists path globs whose hits
# are legit placeholders (e.g. USER.md.example, .env.example). One glob per line;
# blank lines and #-comments ignored. Matched paths are skipped entirely.
#
# Outbound allowlist (--outbound-diff mode only):
#   "$REPO_ROOT/.outbound-allowlist" lists paths (as
#   substring match against "file:line") that are retroactively reviewed and
#   exempt. Format: one entry per line:
#     <path-substring> | <reason> | <d7-review ref> | <date>
#   Blank lines and #-comments ignored.
#
# D7 scan scope (--outbound-diff mode only):
#   D7 looks for outbound network PRIMITIVES in added lines -- it does NOT
#   scan the PII/secret categories below, so it is neither widened nor
#   narrowed by any of them. Two exclusions, both resting on the same fact
#   (the excluded text is never executed, so it cannot open a socket):
#     - non-code files: *.md, .outbound-allowlist, the sweep script itself
#     - comment lines: added lines whose FIRST non-blank characters are
#       '#', '//', '/*' or '*'. Anchored, so a trailing comment or a marker
#       inside a string still gets scanned. (DGN-1064)
#
# D7 review bypass (--outbound-diff mode only):
#   If env var D7_REVIEW_TICKET is set to a worklog ticket filename (e.g.
#   DGN-999-my-ticket.md), the sweep verifies that
#     "$REPO_ROOT/worklog/<ticket>" contains "d7-review:"
#   and if so treats ALL hits as reviewed (prints D7-REVIEWED note, exits 0).
#   Missing ticket file or missing "d7-review:" record -> still blocked.
#
# Categories (1-9):
#   1 telegram bot tokens        [0-9]{8,10}:[A-Za-z0-9_-]{35}
#   2 owner Telegram ID          (from ~/.dogany/sweep-identity)
#   3 owner emails               (from ~/.dogany/sweep-identity)
#   4 owner name                 (from ~/.dogany/sweep-identity)
#   5 machine paths              (from ~/.dogany/sweep-identity)
#   6 generic keys               sk-... / ghp_... / AKIA...
#   7 .env-style secret lines    (TOKEN|SECRET|API_KEY|PASSWORD)=<8+ chars>
#   8 forbidden tracked files    .env / *.db / sessions.json  (must never track)
#   9 git-identity metadata      real-name / machine-local author|committer on
#                                any ref (git log --all); owner-name portion
#                                from ~/.dogany/sweep-identity
#
# Private owner-backup exemption (DGN-808):
#   A PRIVATE single-owner backup repo -- one whose PURPOSE is to carry owner
#   life-data (instance backup mirrors) -- may self-declare via a repo-root
#   marker file `.dogany-private-backup` whose first non-comment line is
#   exactly:
#     declare: private-owner-backup
#   Valid declaration -> owner-PII categories {2,3,4,5,9} are SKIPPED;
#   real-secret categories {1,6,7,8} STAY ENFORCED. No central registry --
#   declarative and per-repo, so any framework user can enable it. Every
#   failure direction is CLOSED: missing / unreadable / malformed marker ->
#   full sweep. A repo shipping scripts/publish.sh has a public-export path
#   and can NEVER self-exempt (marker refused, full sweep). D7 outbound-diff
#   mode is unaffected by the exemption.
#
# Owner-specific identity (cats 2-5 + the owner-name part of cat9) is NOT
# embedded in this tracked script -- it is sourced at runtime from an UNTRACKED
# machine-local file so that no owner PII lives in framework source. Resolution
# order: "$REPO_ROOT/.sweep-identity" (repo-relative override, gitignored) then
# "$HOME/.dogany/sweep-identity". See scripts/sweep-identity.example for the
# format. If the file is absent (or a required var is empty) cats 2-5 and the
# owner-name part of cat9 are DISABLED with a loud warning (fail-open on
# owner-cats); the generic secret/token/key/env/forbidden-file categories and
# the machine-local part of cat9 still run and still fail-closed.
#
# cat9 identity allowlist (two sources, exact-string match only):
#   (a) the deterministic publish identity parsed LIVE from the scanned repo's
#       own scripts/publish.sh (SNAP_NAME / SNAP_EMAIL) -- publish.sh pins that
#       identity onto the public orphan snapshot, so it is tool-owned output;
#   (b) <repo>/.sweep-identity-allow, one documented entry per line:
#         Name <email> | reason | ref | date
#       Entries without a '| reason' field are ignored. Keep the file
#       UNTRACKED (see cat9 section notes below).
# Unknown identities still hard-block unless the repo has a valid private-owner-backup
# exemption (DGN-808), in which case cat9 is skipped entirely for that repo.

set -u

# ---- args -------------------------------------------------------------------
MODE="tracked"
REPO=""
OUTBOUND_BASE=""
for a in "$@"; do
  case "$a" in
    --staged) MODE="staged" ;;
    --outbound-diff)
      MODE="outbound-diff"
      # next arg is the base-ref; consume it below
      ;;
    -h|--help)
      sed -n '2,88p' "$0"; exit 0 ;;  # header doc lines 2-88
    -*)
      # if we are waiting for the base-ref, reject double-flags
      if [ "$MODE" = "outbound-diff" ] && [ -z "$OUTBOUND_BASE" ]; then
        echo "ERROR: --outbound-diff requires a <base-ref> argument" >&2; exit 2
      fi
      echo "unknown flag: $a" >&2; exit 2 ;;
    *)
      if [ "$MODE" = "outbound-diff" ] && [ -z "$OUTBOUND_BASE" ]; then
        OUTBOUND_BASE="$a"
      else
        REPO="$a"
      fi
      ;;
  esac
done

if [ "$MODE" = "outbound-diff" ] && [ -z "$OUTBOUND_BASE" ]; then
  echo "ERROR: --outbound-diff requires a <base-ref> argument" >&2; exit 2
fi
REPO="${REPO:-$(pwd)}"

if ! git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: not a git repo: $REPO" >&2
  exit 2
fi
REPO_ROOT="$(git -C "$REPO" rev-parse --show-toplevel)"

# ---- outbound-diff mode (D7 tripwire) ---------------------------------------
if [ "$MODE" = "outbound-diff" ]; then
  OUTBOUND_ALLOWLIST="$REPO_ROOT/.outbound-allowlist"
  # Primitive ERE per D7 spec (D2 discipline): call-shapes only.
  # bare 'fetch' explicitly excluded per spec.
  # \b before 'curl' prevents word-suffix false positives (e.g. leg_curl,
  # bicep_curl, hammer_curl in workout spec docs).
  OUTBOUND_ERE='\bcurl |wget |urllib\.request|requests\.(get|post|put|delete|patch)|httpx\.'

  # D7 scope: executable code files only. Markdown (.md) and prose/config
  # files cannot initiate network connections; scanning them for the ERE
  # produces prose-quote false positives (doctrine text, ticket bodies,
  # archive handoffs). Non-code paths excluded:
  #   *.md          -- documentation, worklog tickets, reports, backlog
  #   .outbound-allowlist -- allowlist itself contains ERE text in comments
  #   scripts/secret-sweep.sh -- this script defines the ERE (self-hit)
  #   agents/.template/scripts/secret-sweep.sh -- template copy, same self-hit
  # This does NOT blanket-exclude any directory; only the .md extension and
  # the named non-code files. Real outbound code (.py/.sh/.js/etc) is
  # unaffected. DGN-377 d7-review 2026-07-17; DGN-668 template copy 2026-08-03.
  _d7_is_nonprose() {  # returns 0 (skip) when file should be excluded from D7 scan
    local f="$1"
    case "$f" in
      *.md) return 0 ;;
      .outbound-allowlist) return 0 ;;
      scripts/secret-sweep.sh) return 0 ;;
      agents/.template/scripts/secret-sweep.sh) return 0 ;;
    esac
    return 1
  }

  # D7 scope, second half (DGN-1064): a COMMENT line is excluded for the SAME
  # reason .md prose is -- it is never executed, so it cannot initiate a
  # network connection. Without this, D7's own doc-and-test prose trips the
  # gate: the metal instance was blocked for 885 commits by one line reading
  # "# No real Telegram send: curl is stubbed." in a test file.
  #
  # The marker must be the FIRST non-blank thing on the added line. That
  # anchoring is what keeps this from being a hole:
  #   "# curl https://x"            -> skipped   (inert text)
  #   "curl https://x   # fetch"    -> SCANNED   (trailing comment; still hits)
  #   "echo \"# curl https://x\""   -> SCANNED   (marker sits inside a string)
  # Nothing can be smuggled in commented-out and quietly switched on later:
  # un-commenting produces a NEW added line WITHOUT the marker, and that line
  # is scanned by this same gate on the push that carries it.
  #
  # Markers cover the comment syntax of every language in the tree: '#'
  # (sh/python/ruby/yaml/toml/make), '//' and '/*' and continuation '*'
  # (js/ts/go/rust/c/java). Deliberately NOT included: ';' and '--', which
  # begin executable lines in shell contexts.
  _d7_is_comment() {  # returns 0 (skip) when the added line is a comment line
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"   # strip leading whitespace
    case "$s" in
      '#'*|'//'*|'/*'*|'*'*) return 0 ;;
    esac
    return 1
  }

  echo "secret-sweep :: repo=$REPO_ROOT mode=outbound-diff base=$OUTBOUND_BASE"

  # Any added lines at all? grep -q short-circuits and never materialises the
  # diff in a shell variable (a 885-commit diff is ~200k lines).
  if ! git -C "$REPO_ROOT" diff "${OUTBOUND_BASE}..HEAD" -- 2>/dev/null \
       | grep '^+' | grep -qv '^+++' ; then
    echo "RESULT: D7-CLEAN (no added lines in diff)"
    exit 0
  fi

  # Scan added lines for outbound primitives.
  # Format each hit as "file::line_context" for allowlist matching.
  # We need file context from the diff, so re-parse with file headers.
  #
  # PRE-FILTER (DGN-1064): one grep over the whole diff keeps only the
  # '+++ b/' file headers (needed for file tracking) and the lines that match
  # OUTBOUND_ERE. The shell loop below then walks a handful of lines instead
  # of all ~200k. This is match-for-match identical to the old per-line
  # `printf | grep -qE` -- same grep, same ERE, and the ERE is unanchored so
  # the leading '+' of a diff line cannot change whether it matches -- but it
  # drops two forks per added line, which was this gate's entire cost.
  # Measured on the metal instance (885 commits ahead): 188s -> ~1s.
  DIFF_OUTPUT="$(git -C "$REPO_ROOT" diff "${OUTBOUND_BASE}..HEAD" -- 2>/dev/null \
    | grep -E '^\+\+\+ b/|'"$OUTBOUND_ERE")"
  CURRENT_FILE=""
  D7_HITS=""
  while IFS= read -r dline; do
    # Track current file from diff header
    case "$dline" in
      '+++ b/'*)
        CURRENT_FILE="${dline#+++ b/}"
        ;;
      '+'*)
        # Skip non-code files (markdown, allowlist, sweep script itself)
        _d7_is_nonprose "$CURRENT_FILE" && continue
        # Added line (not the +++ file header). It already matched
        # OUTBOUND_ERE in the pre-filter above.
        content="${dline#+}"
        # Skip comment lines -- inert prose inside a code file.
        _d7_is_comment "$content" && continue
        D7_HITS="${D7_HITS}${CURRENT_FILE}::${content}"$'\n'
        ;;
    esac
  done <<EOF
$DIFF_OUTPUT
EOF

  if [ -z "$D7_HITS" ]; then
    echo "RESULT: D7-CLEAN (no outbound primitive hits in added lines)"
    exit 0
  fi

  # Load outbound allowlist (substring match against "file::content" entries)
  OB_ALLOW=""
  if [ -f "$OUTBOUND_ALLOWLIST" ]; then
    while IFS= read -r aline; do
      aline="${aline%%#*}"        # strip comment
      # strip leading/trailing whitespace
      aline="$(printf '%s' "$aline" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      # allowlist entries have format: <path-substring> | <reason> | <ref> | <date>
      # extract the path-substring (first field before |)
      path_part="${aline%%|*}"
      path_part="$(printf '%s' "$path_part" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      [ -n "$path_part" ] && OB_ALLOW="${OB_ALLOW}${path_part}"$'\n'
    done < "$OUTBOUND_ALLOWLIST"
  fi

  # Filter hits through allowlist (substring match on file path)
  UNALLOWLISTED_HITS=""
  ALLOWLISTED_COUNT=0
  while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    file_part="${hit%%::*}"
    matched=0
    if [ -n "$OB_ALLOW" ]; then
      while IFS= read -r pat; do
        [ -z "$pat" ] && continue
        case "$file_part" in
          *"$pat"*) matched=1; break ;;
        esac
      done <<PATEOF
$OB_ALLOW
PATEOF
    fi
    if [ "$matched" = "1" ]; then
      ALLOWLISTED_COUNT=$(( ALLOWLISTED_COUNT + 1 ))
    else
      UNALLOWLISTED_HITS="${UNALLOWLISTED_HITS}${hit}"$'\n'
    fi
  done <<EOF
$D7_HITS
EOF

  if [ -z "$UNALLOWLISTED_HITS" ]; then
    echo "RESULT: D7-CLEAN (all hits allowlisted; allowlisted=$ALLOWLISTED_COUNT)"
    exit 0
  fi

  # Check D7_REVIEW_TICKET bypass
  if [ -n "${D7_REVIEW_TICKET:-}" ]; then
    TICKET_FILE="$REPO_ROOT/worklog/${D7_REVIEW_TICKET}"
    if [ ! -f "$TICKET_FILE" ]; then
      echo "D7-REVIEW-ERROR: ticket file not found: $TICKET_FILE"
      echo "(D7_REVIEW_TICKET set but file missing -- push still blocked)"
    elif grep -q "d7-review:" "$TICKET_FILE"; then
      echo "D7-REVIEWED: ticket $D7_REVIEW_TICKET contains d7-review: record"
      echo "  All outbound hits treated as reviewed. Hits found:"
      while IFS= read -r hit; do
        [ -z "$hit" ] && continue
        echo "  D7-OUTBOUND-HIT (reviewed): $hit"
      done <<EOF
$UNALLOWLISTED_HITS
EOF
      exit 0
    else
      echo "D7-REVIEW-ERROR: ticket $D7_REVIEW_TICKET found but contains no d7-review: record"
      echo "(push still blocked)"
    fi
  fi

  # Unallowlisted hits, no valid review bypass -> BLOCK
  echo "RESULT: D7-BLOCKED ($( printf '%s\n' "$UNALLOWLISTED_HITS" | grep -c . ) unallowlisted hit(s))"
  echo "-- D7-OUTBOUND-HIT lines --"
  while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    echo "  D7-OUTBOUND-HIT: $hit"
  done <<EOF
$UNALLOWLISTED_HITS
EOF
  echo "STOP: outbound network primitive added without D7 review."
  echo "  Either: (a) add an allowlist entry in .outbound-allowlist with a d7-review ref,"
  echo "         (b) set D7_REVIEW_TICKET=<worklog-ticket> with a d7-review: record in that ticket,"
  echo "         (c) remove the outbound call or gate it behind opt-in."
  exit 1
fi

# ---- private owner-backup exemption (DGN-808) -------------------------------
# Declarative per-repo marker, no central registry: a private single-owner
# backup repo self-declares via `.dogany-private-backup` at the repo root.
# Valid declaration -> owner-PII cats {2,3,4,5,9} skipped; real-secret cats
# {1,6,7,8} always enforced. Every failure direction is CLOSED:
#   - marker absent            -> full sweep (status quo)
#   - marker unreadable        -> full sweep + loud warning
#   - marker malformed         -> full sweep + loud warning
#   - repo ships scripts/publish.sh (public-export path) -> exemption REFUSED,
#     full sweep + loud warning (a publishing repo must never self-exempt)
# The declaration line is matched EXACTLY (no glob/regex) so the marker cannot
# be satisfied by accident; comments (#) and blank lines are permitted above it.
PB_MARKER="$REPO_ROOT/.dogany-private-backup"
PB_DECLARATION="declare: private-owner-backup"
PRIVATE_BACKUP_EXEMPT=0
if [ -e "$PB_MARKER" ]; then
  PB_FIRST=""
  # Defect3 (hardening): symlink as marker -> fail-closed (cannot be an owned
  # regular file if it is a symlink; could point outside the repo boundary).
  if [ -L "$PB_MARKER" ]; then
    {
      echo "secret-sweep: WARNING -- .dogany-private-backup is a symlink (must be a"
      echo "  regular file). Exemption REFUSED -- FULL sweep (fail-closed)."
    } >&2
  elif [ -f "$PB_MARKER" ] && [ -r "$PB_MARKER" ]; then
    PB_FIRST="$(grep -vE '^[[:space:]]*(#|$)' "$PB_MARKER" 2>/dev/null | head -1 \
      | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  fi
  if [ -L "$PB_MARKER" ]; then
    : # already warned above; PRIVATE_BACKUP_EXEMPT stays 0
  elif [ "$PB_FIRST" != "$PB_DECLARATION" ]; then
    {
      echo "secret-sweep: WARNING -- .dogany-private-backup present but NOT a valid"
      echo "  declaration (first non-comment line must be exactly:"
      echo "  '$PB_DECLARATION'). Exemption REFUSED -- FULL sweep (fail-closed)."
    } >&2
  else
    # Defect1 (blocker): publish.sh presence check MUST cover both the working-tree
    # (-e) AND the git index (ls-files --error-unmatch). A tracked publish.sh that is
    # deleted in the working-tree but still in the index would otherwise let the
    # exemption activate on a repo that has a public-export path.
    PB_PUBLISH_PRESENT=0
    if [ -e "$REPO_ROOT/scripts/publish.sh" ]; then
      PB_PUBLISH_PRESENT=1
    elif git -C "$REPO_ROOT" ls-files --error-unmatch scripts/publish.sh >/dev/null 2>&1; then
      PB_PUBLISH_PRESENT=1
    fi
    if [ "$PB_PUBLISH_PRESENT" = "1" ]; then
      {
        echo "secret-sweep: WARNING -- .dogany-private-backup declared on a repo that"
        echo "  ships scripts/publish.sh (public-export path). Exemption REFUSED --"
        echo "  FULL sweep (fail-closed)."
      } >&2
    else
      PRIVATE_BACKUP_EXEMPT=1
      echo "note: private owner-backup exemption ACTIVE (.dogany-private-backup declared)"
      echo "  owner-PII cats {2,3,4,5,9} skipped; cats {1,6,7,8} still enforced (DGN-808)"
    fi
  fi
fi

# ---- file list (bash 3.2: newline-delimited, no mapfile) --------------------
if [ "$MODE" = "staged" ]; then
  # staged, added/copied/modified only (skip deletions)
  FILE_LIST="$(git -C "$REPO_ROOT" diff --cached --name-only --diff-filter=ACM)"
else
  FILE_LIST="$(git -C "$REPO_ROOT" ls-files)"
fi
FILE_COUNT=0
[ -n "$FILE_LIST" ] && FILE_COUNT="$(printf '%s\n' "$FILE_LIST" | grep -c .)"

# ---- allowlist --------------------------------------------------------------
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

# ---- owner identity (sourced at runtime; NEVER embedded here) ----------------
# Owner-specific detection patterns (cats 2-5 and the owner-name part of cat9)
# come from an UNTRACKED machine-local identity file so no owner PII lives in
# this tracked script. Resolve "$REPO_ROOT/.sweep-identity" (gitignored repo
# override) first, else "$HOME/.dogany/sweep-identity". The file is a plain
# shell-sourceable file defining:
#   SWEEP_OWNER_TG_ID      SWEEP_OWNER_EMAIL_PAT  SWEEP_OWNER_NAME_PAT
#   SWEEP_MACHINE_PATH     SWEEP_OWNER_IDENT_PAT
# See scripts/sweep-identity.example for the format.
#
# Fail-open (loud) on owner-cats: if the file is missing or any required var is
# empty, cats 2-5 and the owner-name part of cat9 are DISABLED and a loud
# warning is printed. The generic secret cats (1,6,7,8), and the machine-local
# part of cat9 ('@*.local'), still run and still fail-closed. Missing owner
# identity NEVER blocks/exits by itself -- generic protection is unaffected.
OWNER_IDENT_ACTIVE=0
SWEEP_OWNER_TG_ID=""
SWEEP_OWNER_EMAIL_PAT=""
SWEEP_OWNER_NAME_PAT=""
SWEEP_MACHINE_PATH=""
SWEEP_OWNER_IDENT_PAT=""

SWEEP_IDENT_FILE=""
if [ -f "$REPO_ROOT/.sweep-identity" ]; then
  SWEEP_IDENT_FILE="$REPO_ROOT/.sweep-identity"
elif [ -f "$HOME/.dogany/sweep-identity" ]; then
  SWEEP_IDENT_FILE="$HOME/.dogany/sweep-identity"
fi

if [ -n "$SWEEP_IDENT_FILE" ]; then
  # shellcheck disable=SC1090
  . "$SWEEP_IDENT_FILE"
fi

if [ -n "${SWEEP_OWNER_TG_ID:-}" ] && [ -n "${SWEEP_OWNER_EMAIL_PAT:-}" ] \
   && [ -n "${SWEEP_OWNER_NAME_PAT:-}" ] && [ -n "${SWEEP_MACHINE_PATH:-}" ] \
   && [ -n "${SWEEP_OWNER_IDENT_PAT:-}" ]; then
  OWNER_IDENT_ACTIVE=1
else
  {
    echo "==============================================================================="
    echo "secret-sweep: WARNING -- no owner identity file (~/.dogany/sweep-identity)"
    echo "  owner-PII categories cat2-5 and the owner-name part of cat9 are DISABLED."
    echo "  Generic token/key/env/forbidden-file categories are STILL ACTIVE and still"
    echo "  fail-closed. Provide the identity file for full owner-PII protection"
    echo "  (copy scripts/sweep-identity.example to ~/.dogany/sweep-identity and fill it)."
    echo "==============================================================================="
  } >&2
fi

# ---- patterns (category label -> ERE) ---------------------------------------
# Use printf-safe literals; keep each pattern a single ERE for grep -E.
# Owner cats 2-5 are populated from the sourced identity vars (empty when the
# identity file is absent -> those cats are skipped in the scan loop below).
declare -a CAT_NAME CAT_PAT
CAT_NAME[1]="telegram-token";  CAT_PAT[1]='[0-9]{8,10}:[A-Za-z0-9_-]{35}'
CAT_NAME[2]="owner-tg-id";     CAT_PAT[2]="${SWEEP_OWNER_TG_ID}"
CAT_NAME[3]="owner-email";     CAT_PAT[3]="${SWEEP_OWNER_EMAIL_PAT}"
CAT_NAME[4]="owner-name";      CAT_PAT[4]="${SWEEP_OWNER_NAME_PAT}"
CAT_NAME[5]="machine-path";    CAT_PAT[5]="${SWEEP_MACHINE_PATH}"
CAT_NAME[6]="generic-key";     CAT_PAT[6]='sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}'
CAT_NAME[7]="env-secret-line"; CAT_PAT[7]='(TOKEN|SECRET|API_KEY|PASSWORD)=[^[:space:]]{8,}'
CAT_NAME[8]="forbidden-tracked-file"   # filename-based, no content pattern
CAT_NAME[9]="git-identity"             # commit author/committer metadata, not file content

# ---- scan (CAT_COUNT is indexed 1-8, fine in bash 3.2) ----------------------
CAT_COUNT=(0 0 0 0 0 0 0 0 0 0)   # indices 1..9 used
TOTAL=0
HITLINES=""

record() {  # $1=cat idx  $2=file  $3=detail
  CAT_COUNT[$1]=$(( ${CAT_COUNT[$1]:-0} + 1 ))
  TOTAL=$(( TOTAL + 1 ))
  HITLINES="${HITLINES}  [cat${1} ${CAT_NAME[$1]}] ${2}${3:+ :: }${3}"$'\n'
}

while IFS= read -r f; do
  [ -z "$f" ] && continue
  if is_allowed "$f"; then continue; fi
  abs="$REPO_ROOT/$f"

  # cat 8: forbidden filenames (tracked at all = violation)
  base="$(basename "$f")"
  case "$base" in
    .env|sessions.json) record 8 "$f" "" ;;
  esac
  case "$f" in
    *.db|*.db-wal|*.db-shm) record 8 "$f" "" ;;
  esac
  # gap-DGN-868: .bak and backup-variant DB files are also forbidden.
  # Two missed cases: (a) plain *.bak (e.g. agent.conf.bak), (b) *.db.bak*
  # (e.g. lifekit.db.bak_<tag>_<date>) -- the *.db pattern above does not match
  # because the file has an additional .bak* suffix after .db.
  case "$base" in
    *.bak|*.bak_*|*.bak-*) record 8 "$f" "(bak variant)" ;;
  esac
  case "$f" in
    *.db.bak|*.db.bak_*|*.db.bak-*) record 8 "$f" "(db-bak variant)" ;;
  esac

  # content scans only for existing regular files (skip binaries/gone)
  [ -f "$abs" ] || continue
  if grep -Iq . "$abs" 2>/dev/null; then :; else continue; fi   # skip binary

  for i in 1 2 3 4 5 6 7; do
    # Private owner-backup exemption (DGN-808): owner-PII content cats {2-5}
    # skipped ONLY under a valid declaration; {1,6,7} always run.
    if [ "$PRIVATE_BACKUP_EXEMPT" = "1" ]; then
      case "$i" in 2|3|4|5) continue ;; esac
    fi
    # Owner cats 2-5 carry an empty pattern when no identity file was sourced;
    # skip them (an empty ERE would otherwise match every line).
    [ -z "${CAT_PAT[$i]}" ] && continue
    m="$(grep -nE "${CAT_PAT[$i]}" "$abs" 2>/dev/null | head -3)"
    [ -z "$m" ] && continue
    if [ "$i" = "6" ]; then
      # cat6 doc-quote exemption (DGN-1058): AKIAIOSFODNN7EXAMPLE is the
      # AWS-RESERVED documentation example key (published in AWS's own docs;
      # by construction it can never be a live credential). Docs that QUOTE it
      # (this sweep's regression-test doc, tickets citing sweep output) would
      # otherwise jam the gate forever -- the sweep documenting itself must not
      # DoS the push path. Exact-string only, and NOT a line/path allowlist:
      # the example value is REMOVED from each matched line and the remainder
      # is RE-CHECKED against the cat6 pattern, so a line carrying BOTH the
      # example and a real-shaped key still blocks.
      m="$(printf '%s\n' "$m" | sed 's/AKIAIOSFODNN7EXAMPLE//g' \
        | grep -E "${CAT_PAT[$i]}")"
      [ -z "$m" ] && continue
    fi
    if [ "$i" = "7" ]; then
      # cat7 is the noisiest: filter obvious NON-secrets (placeholders / code).
      # Drop matched lines whose value is a placeholder or a shell expression,
      # keep only lines that still look like a real inlined secret value.
      # ["'] after = is an optional opening quote; tolerate it before the value.
      # TEST-ONLY- (DGN-1058): the UNIFORM fake-fixture prefix for test
      # harnesses (e.g. TELEGRAM_BOT_TOKEN=TEST-ONLY-token123). Fixtures are
      # made recognizable BY FORM instead of path-allowlisted in .sweepignore:
      # a path allowlist would also pass a real credential later added to the
      # same file, while the form filter passes only values that self-declare
      # as fixtures. A real secret smuggled behind the prefix would not be a
      # working credential for its consumer (same accepted risk class as the
      # existing dummy/test placeholder prefixes).
      m="$(printf '%s\n' "$m" \
        | grep -Ev '=["'"'"']?(your_|changeme|change_me|xxx|placeholder|example|dummy|test|TEST-ONLY-|<)' \
        | grep -Ev '=["'"'"']?(\$\(|\$\{|\$[A-Za-z_])' \
        | grep -Ev '=["'"'"']["'"'"'] *$' )"
      [ -z "$m" ] && continue
    fi
    # collapse to first matching line number for the summary
    firstln="$(echo "$m" | head -1 | cut -d: -f1)"
    record "$i" "$f" "line ${firstln}"
  done
done <<EOF
$FILE_LIST
EOF

# ---- cat 9: commit metadata identity (author/committer of EVERY commit, -----
# ---- plus tagger of every annotated tag -- DGN-252 S3')                  -----
# File contents are covered above; git history metadata is a separate leak
# surface (found live: initial commit authored as the owner's real name +
# machine hostname). The public identity is a pinned deterministic bot identity
# (owner decision at the v1.0.1 history rewrite, 2026-07-04), so flag
# real-name / machine-local / agent-machine identities only.
#
# cat9 allowlist (DGN-604 reconcile, 2026-07-30). Threat model: real identity
# reaching a PUBLIC history. publish.sh isolates the public repo to a single
# ORPHAN snapshot commit with a pinned deterministic bot identity (gate 7,
# post-push parents=0 verify), so private canonical commit authors
# structurally never reach public; the export-tree sweep (publish.sh gate 3)
# still catches identity STRINGS in shipping content via cats 3/4/5. Blocking
# a private/canonical push on history authors was therefore over-reach.
# Two allow sources, both anchored so they cannot travel to a public clone:
#   (a) tool-owned publish identity, parsed LIVE from the scanned repo's own
#       scripts/publish.sh (SNAP_NAME/SNAP_EMAIL). Never duplicated here, so
#       the two tools cannot drift; no publish.sh in the repo -> no allowance.
#       (A public clone shipping publish.sh allowing its own pinned bot
#       identity is by definition the intended public identity.)
#   (b) $REPO_ROOT/.sweep-identity-allow -- explicit documented entries:
#         Name <email> | reason | ref | date
#       Entries without a '| reason' field are IGNORED (documentation is
#       mandatory). Keep this file UNTRACKED: it has no publish owner so it
#       cannot ship through the curated export, and if it were ever tracked
#       its own content trips cat4 (owner-name) until deliberately reviewed
#       into .sweepignore -- a forced review point.
# A hit line passes only if, after removing every allowlisted EXACT identity
# string, nothing on the line still matches IDENT_PAT. Unknown identities
# hard-block in every mode; no other category is affected.
#
# IDENT_PAT is built at runtime: the machine-local pattern '@*.local' is NOT PII
# and stays hardcoded; the owner-name/username/hostname portion comes from the
# sourced SWEEP_OWNER_IDENT_PAT (empty when no identity file -> owner-name part
# of cat9 disabled, machine-local part still active).
IDENT_MACHINE_LOCAL='@[^ ]*\.local'
if [ "$OWNER_IDENT_ACTIVE" = "1" ]; then
  IDENT_PAT="${SWEEP_OWNER_IDENT_PAT}|${IDENT_MACHINE_LOCAL}"
else
  IDENT_PAT="${IDENT_MACHINE_LOCAL}"
fi

IDENT_ALLOW=""
PUBLISH_SH="$REPO_ROOT/scripts/publish.sh"
if [ -f "$PUBLISH_SH" ]; then
  SNAP_NAME="$(sed -n 's/^SNAP_NAME="\(.*\)"[[:space:]]*$/\1/p' "$PUBLISH_SH" | head -1)"
  SNAP_EMAIL="$(sed -n 's/^SNAP_EMAIL="\(.*\)"[[:space:]]*$/\1/p' "$PUBLISH_SH" | head -1)"
  if [ -n "$SNAP_NAME" ] && [ -n "$SNAP_EMAIL" ]; then
    IDENT_ALLOW="${IDENT_ALLOW}${SNAP_NAME} <${SNAP_EMAIL}>"$'\n'
  fi
fi
IDENT_ALLOW_FILE="$REPO_ROOT/.sweep-identity-allow"
if [ -f "$IDENT_ALLOW_FILE" ]; then
  while IFS= read -r aline; do
    aline="$(printf '%s' "$aline" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$aline" ] && continue
    case "$aline" in '#'*) continue ;; esac
    case "$aline" in
      *\|*) : ;;
      *) echo "note: cat9 allow entry ignored (missing '| reason' field): $aline" >&2
         continue ;;
    esac
    ident="${aline%%|*}"
    ident="$(printf '%s' "$ident" | sed 's/[[:space:]]*$//')"
    [ -n "$ident" ] && IDENT_ALLOW="${IDENT_ALLOW}${ident}"$'\n'
  done < "$IDENT_ALLOW_FILE"
fi

# Commit author/committer lines PLUS annotated-tag tagger lines (DGN-252 S3'):
# an annotated tag carries its own 'tagger Name <email>' -- an unset git
# identity there defaults to login@host, same leak surface as commits.
# Lightweight tags emit empty tagger fields and simply never match IDENT_PAT.
# Private owner-backup exemption (DGN-808): on a declared private owner-backup
# repo the commit identity IS the owner's by construction -- cat9 skipped
# entirely (both owner-name and machine-local parts). All other repos: cat9
# runs unchanged.
IDENT_RAW=""
if [ "$PRIVATE_BACKUP_EXEMPT" != "1" ]; then
  IDENT_RAW="$( { git -C "$REPO_ROOT" log --all --format='%h %an <%ae> / %cn <%ce>' 2>/dev/null;
    git -C "$REPO_ROOT" for-each-ref refs/tags --format='%(refname:short) %(taggername) %(taggeremail)' 2>/dev/null; } \
    | grep -E "$IDENT_PAT")"
fi
IDENT_ALLOWED_COUNT=0
IDENT_HITS=""
if [ -n "$IDENT_RAW" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    scrub="$line"
    if [ -n "$IDENT_ALLOW" ]; then
      while IFS= read -r ident; do
        [ -z "$ident" ] && continue
        scrub="${scrub//"$ident"/}"
      done <<ALLOWEOF
$IDENT_ALLOW
ALLOWEOF
    fi
    if printf '%s\n' "$scrub" | grep -qE "$IDENT_PAT"; then
      IDENT_HITS="${IDENT_HITS}${line}"$'\n'
    else
      IDENT_ALLOWED_COUNT=$(( IDENT_ALLOWED_COUNT + 1 ))
    fi
  done <<EOF
$IDENT_RAW
EOF
fi
IDENT_HITS="$(printf '%s' "$IDENT_HITS" | head -5)"
if [ -n "$IDENT_HITS" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    record 9 "(commit)" "$line"
  done <<EOF
$IDENT_HITS
EOF
fi

# ---- report -----------------------------------------------------------------
# Reduced-mode visibility (DGN-1058): a reduced sweep must NEVER look like a
# full sweep. "clean" and "not checked" being indistinguishable is this repo's
# recurring failure class (DGN-1043: an alarm nobody can read gets ignored).
# The scope suffix rides the RESULT line itself so any caller that surfaces
# only that one line (pre-push hook success path) still shows what was skipped.
SCOPE_SUFFIX=""
if [ "$PRIVATE_BACKUP_EXEMPT" = "1" ]; then
  SCOPE_SUFFIX=" [reduced sweep: cats 1,6,7,8 enforced; owner-PII cats 2,3,4,5,9 SKIPPED -- DGN-808]"
fi
echo "secret-sweep :: repo=$REPO_ROOT mode=$MODE files=$FILE_COUNT"
if [ "$IDENT_ALLOWED_COUNT" -gt 0 ]; then
  echo "note: cat9 allowlisted commit-identity line(s) skipped: $IDENT_ALLOWED_COUNT"
fi
if [ "$TOTAL" -eq 0 ]; then
  echo "RESULT: CLEAN (0 hits)${SCOPE_SUFFIX}"
  exit 0
fi

echo "RESULT: DIRTY ($TOTAL hit(s))${SCOPE_SUFFIX}"
echo "-- category summary --"
for i in 1 2 3 4 5 6 7 8 9; do
  c="${CAT_COUNT[$i]:-0}"
  [ "$c" -gt 0 ] && printf "  cat%s %-24s %d\n" "$i" "${CAT_NAME[$i]}" "$c"
done
echo "-- hits --"
printf "%s" "$HITLINES"
echo "STOP: do not push until clean (or allowlist legit placeholders in .sweepignore)."
exit 1
