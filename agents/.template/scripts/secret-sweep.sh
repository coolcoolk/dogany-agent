#!/bin/bash
# secret-sweep.sh -- pre-public-push SAFETY GATE for Dogany repos (DGN-076).
#
# Scans a target repo's TRACKED files for personal data / secrets / forbidden
# files that must never enter a public git history. Exit 0 = clean, 1 = hit(s).
#
# Usage:
#   secret-sweep.sh [<repo-dir>]                      scan tracked files (git ls-files),
#                                                       content read from the WORKING TREE
#   secret-sweep.sh --ref <commit> [<repo-dir>]       scan the COMMITTED tree of <commit>
#                                                       (content read from git objects, never
#                                                       from disk -- what a push would ship)
#   secret-sweep.sh --staged [<repo>]                 scan staged files only
#   secret-sweep.sh --staged-added-lines [<repo>]     scan only added lines in the staged diff
#   secret-sweep.sh --outbound-diff <base-ref> [<repo>]  D7 tripwire: scan only ADDED
#                                                       lines of git diff <base-ref>..HEAD
#   secret-sweep.sh -h | --help
#
# Committed-tree mode (--ref, DGN-1696): the pre-push hook passes --ref <sha>
#   for every pushed local sha, so the gate sweeps WHAT IS BEING PUSHED, not
#   whatever happens to be dirty on disk (an uncommitted memory write used to
#   block backup pushes of clean commits for days). Under --ref the file
#   list, every file's content, cat8 forbidden names, the .sweepignore
#   allowlist and the DGN-808 private-backup marker (+ its publish.sh
#   refusal) are all read from <commit>'s tree -- an uncommitted marker or
#   .sweepignore edit can never exempt anything. Without --ref the default is
#   UNCHANGED (index file list + working-tree content): publish.sh /
#   publish-bridge.sh / extract-bridge.sh sweep freshly `git add -A`-ed
#   scratch repos that have no commit at all, so a HEAD default would scan
#   zero files there and read as a false CLEAN. To sweep HEAD explicitly:
#   `secret-sweep.sh --ref HEAD <repo>`. --ref is tracked-mode only
#   (rejected with --staged / --staged-added-lines / --outbound-diff); an
#   unresolvable ref exits 2.
#
# Allowlist: a `.sweepignore` file at the repo root lists path globs whose hits
# are legit placeholders (e.g. USER.md.example, .env.example). One glob per line;
# blank lines and #-comments ignored. Matched paths are skipped entirely.
#
# Outbound allowlist (--outbound-diff mode only):
#   "$REPO_ROOT/.outbound-allowlist" lists paths (as
#   substring match against "file:line") that are retroactively reviewed and
#   exempt. Format: one entry per line:
#     <path-substring> | <reason-token> | <free-text> | <d7-review ref> | <date>
#   reason-token (DGN-1518, mirrors the DGN-1507 reason-token idiom) is one
#   of: false-positive | reviewed-call | fixture | other -- and <date> is
#   YYYY-MM-DD. Every push reports entries=N broken down by token, so a
#   swelling false-positive count is visible instead of silently degrading
#   the gate's sensitivity. An entry missing the token or a valid date is
#   still fully honored (counted "legacy"/"malformed-timestamp" and reported,
#   never blocked on -- metadata gaps can only ever leave enforcement AS
#   PERMISSIVE as before, never MORE permissive nor MORE blocking).
#   Blank lines and #-comments ignored.
#   D7_OUTBOUND_ALLOWLIST_EXTRA (optional, DGN-1361): an ABSOLUTE path to a
#   second allowlist file, same format, merged ADDITIVELY with the one above.
#   For scanning a repo you do not maintain (e.g. reviewing a public export
#   from your own reviewer workspace) -- lets the reviewer keep entries in
#   their own tree instead of committing gate artifacts into the scanned
#   repo. The scanned repo's own "$REPO_ROOT/.outbound-allowlist" (if any)
#   is still read exactly as before; a missing/unreadable/relative extra
#   path is a no-op (fewer allowlist entries only ever means MORE hits stay
#   blocked, never fewer), never a silent free pass.
#   Shipped fragments (DGN-1860), read with no env needed, merged ADDITIVELY
#   with the two sources above, same line format:
#     "$REPO_ROOT/scripts/outbound-allowlist.framework" -- framework layer.
#       Shipped in the instance template, refreshed by every framework
#       update (update.sh 3k5, same channel as this script). Holds only
#       framework files whose outbound primitive has review evidence.
#     "$REPO_ROOT/config/packs/<pack-id>.outbound-allowlist" -- kit layer.
#       Delivered by pack_install.sh from a kit/pack payload's
#       config/outbound-allowlist (replace semantics per pack), so a kit's
#       approvals move with the kit's own skill renames.
#   Fragment entries match the hit's repo-relative path EXACTLY (the
#   instance's own .outbound-allowlist keeps its substring match): an
#   approval shipped to every instance must never clear a different file
#   that merely contains the same path text. A kit-layer entry is honored
#   only for a kit delivery surface (.claude/skills-bundle/, routines/bundle/,
#   service/, database/, knowledge/), never for a framework or owner file.
#   Merge order: owner file, DGN-1361 extra, framework fragment, kit
#   fragments (sorted). The result is a union -- no layer can remove or
#   override another's entry -- so a missing/unreadable fragment only ever
#   leaves MORE hits blocked, and with no fragment present the gate behaves
#   exactly as before DGN-1860.
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
# D7 scope disclosure and comparison errors (DGN-1613):
#   D7 compares two Git trees, not a deployment date -- it can only see
#   primitives added inside base..HEAD; code already present at base is
#   outside every diff and is never scanned by this mode. To keep that
#   truthful instead of implied, every SUCCESSFUL comparison (clean,
#   allowlisted, or ticket-reviewed) prints exactly one line before its
#   result:
#     D7-SCOPE: mode=added-lines base-tree=<OID> head=<OID> detector=heuristic
#       exclusions=markdown,comments,docstrings,scanner-paths,allowlist-file
#       outside-diff=not-scanned historical-review=unknown
#   base-tree/head are the full resolved object IDs actually compared, not
#   the caller-supplied ref spelling -- this also pins the diff so it cannot
#   silently drift if a ref moves mid-scan. The line states input scope only;
#   it is not a coverage percentage or a call count. Both endpoints accept
#   any tree-ish (commit, tag, or a bare tree object, including the empty
#   tree) -- no ancestor relationship is required and no deployment date is
#   read or manufactured.
#   If EITHER endpoint fails to resolve, or the diff itself cannot be
#   produced, that failure takes precedence over every downstream exemption:
#   the scan prints "RESULT: D7-ERROR (comparison unavailable)" to stderr and
#   exits 2. No D7-CLEAN, D7-REVIEWED or D7-SCOPE line is printed on that
#   path -- an unusable comparison must never read as a clean one (a bad
#   base-ref previously produced a false "no added lines" D7-CLEAN).
#
# D7 review bypass (--outbound-diff mode only):
#   If env var D7_REVIEW_TICKET is set to a worklog ticket filename (e.g.
#   DGN-999-my-ticket.md), the sweep verifies that
#     "$REPO_ROOT/worklog/<ticket>" contains "d7-review:"
#   and if so treats ALL hits as reviewed (prints D7-REVIEWED note, exits 0).
#   Missing ticket file or missing "d7-review:" record -> still blocked.
#   DGN-1361: if D7_REVIEW_TICKET is an ABSOLUTE path instead (starts with
#   '/'), it is used AS-IS -- the reviewer's own workspace, independent of
#   the repo under scan (a foreign/public repo has no worklog/ of its own to
#   hold the record). Relative values are completely unaffected: existing
#   callers keep resolving under "$REPO_ROOT/worklog/". The absolute form is
#   restricted to a *.md path: a relative ticket can only ever name a file
#   already confined to $REPO_ROOT/worklog/, but an absolute path can name
#   ANY file on disk -- including this very script, whose header prose above
#   contains the literal string "d7-review:". Without the .md restriction,
#   `D7_REVIEW_TICKET=$(pwd)/scripts/secret-sweep.sh` would satisfy the
#   content check on ANY repo with zero review ever written. Requiring .md
#   keeps the absolute form's reachable set the same shape as the relative
#   form's (a ticket-like document), instead of the whole filesystem.
#
# Categories (1-10):
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
#  10 private-network literals   Tailscale CGNAT (100.64.0.0/10), RFC1918
#                                (10/8, 172.16/12, 192.168/16), *.ts.net
#                                MagicDNS hostnames -- generic (not identity-
#                                file-gated), same standing as cats 1/6/7/8.
#                                A live literal address blocks; the bare
#                                CIDR notation describing a range (e.g. this
#                                comment's own "100.64.0.0/10") does not, nor
#                                does 127.0.0.1 or any public address
#                                (DGN-1276 M3 gap, real owner IP found in a
#                                template fixture with no gate catching it).
#                                Owner-infrastructure PII, not a credential:
#                                skipped under a valid private-owner-backup
#                                declaration (DGN-1696, see below).
#
# Private owner-backup exemption (DGN-808):
#   A PRIVATE single-owner backup repo -- one whose PURPOSE is to carry owner
#   life-data (instance backup mirrors) -- may self-declare via a repo-root
#   marker file `.dogany-private-backup` whose first non-comment line is
#   exactly:
#     declare: private-owner-backup
#   Valid declaration -> owner-PII categories {2,3,4,5,9,10} are SKIPPED
#   (cat10 private-network literals joined the set in DGN-1696: tailnet
#   hostnames / LAN IPs are owner-infrastructure PII, exactly what a private
#   life-data mirror carries); real-secret categories {1,6,7,8} STAY ENFORCED. No central registry --
#   declarative and per-repo, so any framework user can enable it. Every
#   failure direction is CLOSED: missing / unreadable / malformed marker ->
#   full sweep. A repo shipping scripts/publish.sh has a public-export path
#   and can NEVER self-exempt (marker refused, full sweep). D7 outbound-diff
#   mode is unaffected by the exemption. Under --ref the marker is read from
#   the swept commit's tree (DGN-1696): a marker that exists only on disk is
#   reported and NOT honored.
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
#   (b) .sweep-identity-allow, resolved <repo>-local override first else
#       $HOME/.dogany/ default (allowhome, DGN-1104), one documented entry
#       per line: Name <email> | reason | ref | date
#       Entries without a '| reason' field are ignored. Keep the file
#       UNTRACKED (see cat9 section notes below).
# Unknown identities still hard-block unless the repo has a valid private-owner-backup
# exemption (DGN-808), in which case cat9 is skipped entirely for that repo.

set -u

# ---- args -------------------------------------------------------------------
MODE="tracked"
REPO=""
OUTBOUND_BASE=""
SWEEP_REF=""        # DGN-1696: --ref <commit> -> sweep the committed tree
WANT_REF=0
for a in "$@"; do
  if [ "$WANT_REF" = "1" ]; then
    SWEEP_REF="$a"; WANT_REF=0; continue
  fi
  case "$a" in
    --staged) MODE="staged" ;;
    --staged-added-lines) MODE="staged-added-lines" ;;
    --ref) WANT_REF=1 ;;
    --outbound-diff)
      MODE="outbound-diff"
      # next arg is the base-ref; consume it below
      ;;
    -h|--help)
      awk 'NR>1 && /^set -u$/ {exit} NR>1' "$0"; exit 0 ;;  # header doc block
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
if [ "$WANT_REF" = "1" ] || { [ "$WANT_REF" = "0" ] && [ -n "$SWEEP_REF" ] && [ "$MODE" != "tracked" ]; }; then
  echo "ERROR: --ref requires a <commit> argument and is tracked-mode only" >&2; exit 2
fi
REPO="${REPO:-$(pwd)}"

if ! git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: not a git repo: $REPO" >&2
  exit 2
fi
REPO_ROOT="$(git -C "$REPO" rev-parse --show-toplevel)"

# DGN-1696: pin --ref to one commit object up front (a moving ref cannot
# change mid-scan). Unresolvable -> exit 2, never a false CLEAN.
SWEEP_COMMIT=""
if [ -n "$SWEEP_REF" ]; then
  SWEEP_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify --quiet --end-of-options "${SWEEP_REF}^{commit}" 2>/dev/null)"
  if [ -z "$SWEEP_COMMIT" ]; then
    echo "ERROR: --ref '$SWEEP_REF' does not resolve to a commit in $REPO_ROOT" >&2
    exit 2
  fi
fi
# _ref_blob <path>: print the blob at <path> in the swept commit; nonzero when
# absent or not a blob (directory / gitlink).
_ref_blob() {
  git -C "$REPO_ROOT" cat-file blob "${SWEEP_COMMIT}:$1" 2>/dev/null
}

# ---- outbound-diff mode (D7 tripwire) ---------------------------------------
if [ "$MODE" = "outbound-diff" ]; then
  OUTBOUND_ALLOWLIST="$REPO_ROOT/.outbound-allowlist"
  # DGN-1860 shipped fragments (see the header): framework layer + kit layer.
  OB_FW_FRAGMENT="$REPO_ROOT/scripts/outbound-allowlist.framework"
  OB_KIT_FRAGMENTS=""
  if [ -d "$REPO_ROOT/config/packs" ]; then
    for _obk in "$REPO_ROOT"/config/packs/*.outbound-allowlist; do
      [ -f "$_obk" ] && OB_KIT_FRAGMENTS="${OB_KIT_FRAGMENTS}${_obk}"$'\n'
    done
  fi
  # Primitive ERE per D7 spec (D2 discipline): call-shapes only.
  # bare 'fetch' explicitly excluded per spec.
  #
  # DGN-1518: 'curl'/'wget' are polysemous in English prose (exercise-form
  # health domain measured 33/46 outbound hits as this exact false positive:
  # "fingers curl over the bar", "MCP/PIP/DIP curl (deg)", a Python
  # command-allowlist literal "curl ", "wget ", ...). A bare word-boundary
  # match on the token cannot tell "curl" the verb from "curl" the command.
  # COMMAND SHAPE is required instead: curl/wget only counts as a hit when
  # immediately followed by a CLI flag (-x / --long), a URL (http(s)://),
  # a shell variable ($...), or a line-continuation backslash at EOL (a
  # command whose flags/URL sit on the next physical line, e.g.
  # `curl -s -o resp.json -w '%{http_code}' \`  -- the *first* line of that
  # still carries a flag so it matches without needing the continuation
  # case, but a bare `curl \` with everything after the break needs it).
  # Predicate derived from real examples on both sides (DGN-1518 report):
  #   still caught : curl https://x | curl -fsSL https://x | wget https://x
  #                   $(curl -fsSL https://x) | curl -fsSL <H>/get.sh | bash
  #                   curl "https://x/$1" | curl -s -o f -w '%{http_code}' \
  #   no longer hit: "fingers curl over the bar" | "MCP/PIP/DIP curl (deg)"
  #                   "grip curl stand" (word list) | "curl": pose_curl
  #                   "closed-grip curl (zero mesh-through-bar)"
  #                   "wget ", "curl " (Python string-literal command list)
  # \b before 'curl'/'wget' still guards word-suffix false positives
  # (leg_curl, bicep_curl, hammer_curl in workout spec docs).
  OUTBOUND_ERE='\bcurl\b[[:space:]]+(-[A-Za-z]|--[A-Za-z]|"?(https?://|\$)|\\$)|\bwget\b[[:space:]]+(-[A-Za-z]|--[A-Za-z]|"?(https?://|\$)|\\$)|urllib\.request|requests\.(get|post|put|delete|patch)|httpx\.'

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
  # DGN-1860: the shipped allowlist fragments are the same kind of data file
  # as .outbound-allowlist (reason text, never executed): the framework
  # fragment (instance path + its template source), the installed kit
  # fragments, and a kit payload's own source copy.
  _d7_is_nonprose() {  # returns 0 (skip) when file should be excluded from D7 scan
    local f="$1"
    case "$f" in
      *.md) return 0 ;;
      .outbound-allowlist) return 0 ;;
      scripts/outbound-allowlist.framework) return 0 ;;
      agents/.template/scripts/outbound-allowlist.framework) return 0 ;;
      config/packs/*.outbound-allowlist) return 0 ;;
      config/outbound-allowlist|*/config/outbound-allowlist) return 0 ;;
      scripts/secret-sweep.sh) return 0 ;;
      agents/.template/scripts/secret-sweep.sh) return 0 ;;
    esac
    return 1
  }

  # D7 scope, second half (DGN-1064, extended DGN-1518): a COMMENT line is
  # excluded for the SAME reason .md prose is -- it is never executed, so it
  # cannot initiate a network connection. Without this, D7's own doc-and-test
  # prose trips the gate: the metal instance was blocked for 885 commits by
  # one line reading "# No real Telegram send: curl is stubbed." in a test
  # file.
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
  # (js/ts/go/rust/c/java), and '"""' / "'''" (Python docstring delimiter,
  # single-line or opening/closing a block -- DGN-1518). Deliberately NOT
  # included: ';' and '--', which begin executable lines in shell contexts.
  _d7_is_comment() {  # returns 0 (skip) when the added line is a comment line
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"   # strip leading whitespace
    case "$s" in
      '#'*|'//'*|'/*'*|'*'*|'"""'*|"'''"*) return 0 ;;
    esac
    return 1
  }

  # DGN-1518, first half: an INLINE trailing comment ("code # note" or
  # "code // note") is not caught by _d7_is_comment above because the marker
  # is not the first character -- that anchoring is deliberate (see the
  # docstring above it) and must stay, so this is a SEPARATE check layered on
  # top, not a replacement. _d7_strip_trailing_comment removes everything
  # from the first UNQUOTED '#' or '//' onward (simple ["']-balanced quote
  # tracking, no escape-char handling -- a heuristic, not a language parser)
  # and the caller re-tests OUTBOUND_ERE against what remains. This is why
  # "echo \"# curl https://x\"" above still scans: the marker sits INSIDE a
  # quoted string, so the tracker never treats it as a comment start and
  # nothing is stripped. '//' is only treated as a comment start when the
  # preceding character is not ':', so "https://x" surviving inside the
  # unstripped remainder of a real call is never itself mistaken for a
  # comment opener.
  _d7_strip_trailing_comment() {
    printf '%s' "$1" | awk '
      {
        n = length($0); inq = 0; qc = ""; out = "";
        for (i = 1; i <= n; i++) {
          c = substr($0, i, 1);
          if (inq) {
            out = out c;
            if (c == qc) inq = 0;
            continue;
          }
          if (c == "\"" || c == "\x27") { inq = 1; qc = c; out = out c; continue; }
          if (c == "#") { break; }
          if (c == "/" && substr($0, i+1, 1) == "/" && substr($0, i-1, 1) != ":") { break; }
          out = out c;
        }
        print out;
      }'
  }

  # DGN-1518, second half: a multi-line Python docstring's CONTINUATION lines
  # (no marker of their own -- the opening/closing """ lines are already
  # caught by the _d7_is_comment marker-first-char rule above) are tracked
  # with a per-file toggle. DOCSTRING_STATE flips on a line carrying an odd
  # number of \"\"\" or ''' occurrences; a line is treated as inert if EITHER
  # the state going in OR the state coming out of this line is 1 (covers the
  # opening line itself, pure interior lines, and the closing line). Reset to
  # 0 on every new file so state never leaks across files in a multi-file
  # diff.
  DOCSTRING_STATE=0

  echo "secret-sweep :: repo=$REPO_ROOT mode=outbound-diff base=$OUTBOUND_BASE"

  # ---- comparison resolution + single materialized diff (DGN-1613) ----------
  # Resolve both endpoints to full object IDs FIRST. This pins the exact
  # objects compared (a moving ref cannot change mid-scan) and lets a bad
  # base-ref be caught here instead of silently reading as "no added lines"
  # (the false-clean this fix exists to close). Any resolution/diff failure
  # takes precedence over every downstream exemption -- no D7-CLEAN,
  # D7-REVIEWED or D7-SCOPE line on this path.
  D7_BASE_TREE="$(git -C "$REPO_ROOT" rev-parse --verify --end-of-options "${OUTBOUND_BASE}^{tree}" 2>/dev/null)"
  D7_RC=$?
  if [ "$D7_RC" -ne 0 ] || [ -z "$D7_BASE_TREE" ]; then
    echo "RESULT: D7-ERROR (comparison unavailable)" >&2
    exit 2
  fi
  D7_HEAD_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify --end-of-options 'HEAD^{commit}' 2>/dev/null)"
  D7_RC=$?
  if [ "$D7_RC" -ne 0 ] || [ -z "$D7_HEAD_COMMIT" ]; then
    echo "RESULT: D7-ERROR (comparison unavailable)" >&2
    exit 2
  fi

  # Materialize ONE checked diff in a private mktemp file; both downstream
  # consumers read from it instead of each re-invoking git. Cleaned up on
  # every normal exit and on HUP/INT/TERM (which still exit nonzero).
  D7_DIFF_FILE=""
  _d7_cleanup() { [ -n "$D7_DIFF_FILE" ] && rm -f "$D7_DIFF_FILE"; }
  trap _d7_cleanup EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  D7_DIFF_FILE="$(mktemp "${TMPDIR:-/tmp}/d7-diff.XXXXXX" 2>/dev/null)"
  if [ -z "$D7_DIFF_FILE" ] || [ ! -f "$D7_DIFF_FILE" ]; then
    echo "RESULT: D7-ERROR (comparison unavailable)" >&2
    exit 2
  fi

  # --no-ext-diff/--no-textconv/--no-color and explicit src/dst prefixes pin
  # the parser's expected textual format; terminal -- keeps this a pure
  # tree-to-tree diff. Preserves existing rename/detector behavior.
  if ! git -C "$REPO_ROOT" diff --no-ext-diff --no-textconv --no-color \
        --src-prefix=a/ --dst-prefix=b/ "$D7_BASE_TREE" "$D7_HEAD_COMMIT" -- \
        > "$D7_DIFF_FILE" 2>/dev/null; then
    echo "RESULT: D7-ERROR (comparison unavailable)" >&2
    exit 2
  fi

  # The comparison succeeded: disclose its actual scope before any
  # clean/reviewed/blocked result can be produced from it. This is the fix's
  # core deliverable -- a passing scan must say what it looked at.
  echo "D7-SCOPE: mode=added-lines base-tree=$D7_BASE_TREE head=$D7_HEAD_COMMIT detector=heuristic exclusions=markdown,comments,docstrings,scanner-paths,allowlist-file outside-diff=not-scanned historical-review=unknown"

  # DGN-1518 allowlist accountability, mirrors the reason-token idiom DGN-1507
  # designs for the handoff-attribution gate's bypass flag (a small fixed
  # vocabulary so a counter becomes a classifier, not just a tally of "how
  # many times"). Every allowlist entry SHOULD now carry a reason TOKEN as
  # its 2nd field (one of the four below) plus a YYYY-MM-DD timestamp as its
  # LAST field:
  #   <path-substring> | <reason-token> | <free-text> | <d7-review ref> | <date>
  #   false-positive  -- the matched primitive was inert prose/mention, not a
  #                      real call (the DGN-1518 failure class itself)
  #   reviewed-call   -- a REAL primitive, reviewed and accepted (sanctioned
  #                      destination, e.g. an approved health-check GET)
  #   fixture         -- test-only stub, no live network from this path
  #   other           -- anything else; must still be filled in, never blank
  # The token classifies WHY the entry exists because the fix differs by
  # category: a false-positive entry means the DETECTION PREDICATE is still
  # too broad (fix the ERE, like this ticket did for curl/wget) and its count
  # is exactly the "is the gate blunting itself" signal DGN-1518 exists to
  # make visible; a reviewed-call/fixture entry means the SCAN is working as
  # intended and no code change is owed. Lumping them in one undifferentiated
  # count could never tell those apart.
  #
  # This is accounting only -- it never changes which entries clear a hit
  # (that match is still plain path-substring, unchanged). An entry missing
  # the token, or carrying free text instead of one of the four words, is
  # counted as "legacy" and still fully honored: fewer/older-shaped allowlist
  # entries must only ever mean the same or MORE hits stay blocked, never
  # fewer -- so malformed metadata can never tighten OR loosen enforcement,
  # only be reported as a migration nudge.
  OB_TOTAL=0; OB_FALSE_POSITIVE=0; OB_REVIEWED_CALL=0; OB_FIXTURE=0
  OB_OTHER=0; OB_LEGACY=0; OB_BAD_TIMESTAMP=0
  _d7_ob_accounting_file() {
    local file="$1" aline rtoken lastfield
    [ -f "$file" ] || return 0
    while IFS= read -r aline; do
      aline="${aline%%#*}"
      aline="$(printf '%s' "$aline" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      [ -z "$aline" ] && continue
      case "$aline" in *'|'*) : ;; *) continue ;; esac
      OB_TOTAL=$(( OB_TOTAL + 1 ))
      rtoken="$(printf '%s' "$aline" | awk -F'|' '{print $2}' \
        | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      case "$rtoken" in
        false-positive) OB_FALSE_POSITIVE=$(( OB_FALSE_POSITIVE + 1 )) ;;
        reviewed-call)  OB_REVIEWED_CALL=$(( OB_REVIEWED_CALL + 1 )) ;;
        fixture)        OB_FIXTURE=$(( OB_FIXTURE + 1 )) ;;
        other)          OB_OTHER=$(( OB_OTHER + 1 )) ;;
        *)              OB_LEGACY=$(( OB_LEGACY + 1 )) ;;
      esac
      lastfield="$(printf '%s' "$aline" | awk -F'|' '{print $NF}' \
        | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      case "$lastfield" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) : ;;
        *) OB_BAD_TIMESTAMP=$(( OB_BAD_TIMESTAMP + 1 )) ;;
      esac
    done < "$file"
  }
  _d7_ob_accounting_file "$OUTBOUND_ALLOWLIST"
  if [ -n "${D7_OUTBOUND_ALLOWLIST_EXTRA:-}" ] && [ -f "$D7_OUTBOUND_ALLOWLIST_EXTRA" ]; then
    _d7_ob_accounting_file "$D7_OUTBOUND_ALLOWLIST_EXTRA"
  fi
  if [ "$OB_TOTAL" -gt 0 ]; then
    echo "secret-sweep :: outbound-allowlist entries=$OB_TOTAL false-positive=$OB_FALSE_POSITIVE reviewed-call=$OB_REVIEWED_CALL fixture=$OB_FIXTURE other=$OB_OTHER legacy-no-reason-token=$OB_LEGACY malformed-timestamp=$OB_BAD_TIMESTAMP"
    if [ "$OB_LEGACY" -gt 0 ]; then
      echo "  note: $OB_LEGACY legacy entries lack a reason token from {false-positive,reviewed-call,fixture,other} (DGN-1518) -- still honored, migrate when next touched"
    fi
    if [ "$OB_BAD_TIMESTAMP" -gt 0 ]; then
      echo "  note: $OB_BAD_TIMESTAMP entries have a missing/malformed YYYY-MM-DD timestamp in the last field -- still honored, migrate when next touched"
    fi
  fi
  # DGN-1860: shipped fragments are reported on their own line so the owner
  # layer's counts above keep meaning "what this instance's owner approved".
  _d7_ob_count_entries() {
    local file="$1" aline n=0
    [ -f "$file" ] || { echo 0; return 0; }
    while IFS= read -r aline; do
      aline="${aline%%#*}"
      case "$aline" in *'|'*) n=$(( n + 1 )) ;; esac
    done < "$file"
    echo "$n"
  }
  if [ -f "$OB_FW_FRAGMENT" ] || [ -n "$OB_KIT_FRAGMENTS" ]; then
    OB_FW_N="$(_d7_ob_count_entries "$OB_FW_FRAGMENT")"
    OB_KIT_N=0; OB_KIT_NAMES=""
    while IFS= read -r _obk; do
      [ -z "$_obk" ] && continue
      OB_KIT_N=$(( OB_KIT_N + $(_d7_ob_count_entries "$_obk") ))
      _obk="${_obk##*/}"
      OB_KIT_NAMES="${OB_KIT_NAMES:+$OB_KIT_NAMES,}${_obk%.outbound-allowlist}"
    done <<EOF
$OB_KIT_FRAGMENTS
EOF
    echo "secret-sweep :: outbound-allowlist fragments framework=$OB_FW_N kit=$OB_KIT_N${OB_KIT_NAMES:+ (packs: $OB_KIT_NAMES)} match=exact-path"
  fi

  # Any added lines at all? grep -q short-circuits and reads the already
  # materialized diff file (D7_DIFF_FILE above), never a second git invocation.
  if ! grep '^+' "$D7_DIFF_FILE" | grep -qv '^+++' ; then
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
  #
  # DGN-1518 addition: also keep bare '"""'/"'''" lines. A docstring's
  # opening/closing delimiter line usually carries no outbound-primitive text
  # of its own (just the quote marker), so it would otherwise never survive
  # this pre-filter -- and DOCSTRING_STATE (tracked in the loop below) would
  # never learn it toggled, so an interior content line two lines later could
  # not tell it is inside a docstring. Pulling in triple-quote-marker lines
  # too keeps the optimization's shape (still a small subset of the diff,
  # not all ~200k lines) while giving the state machine what it needs.
  DIFF_OUTPUT="$(grep -E '^\+\+\+ b/|'"$OUTBOUND_ERE"'|"""|'"'''"'' "$D7_DIFF_FILE")"
  CURRENT_FILE=""
  D7_HITS=""
  while IFS= read -r dline; do
    # Track current file from diff header
    case "$dline" in
      '+++ b/'*)
        CURRENT_FILE="${dline#+++ b/}"
        DOCSTRING_STATE=0   # never leak docstring state across files
        ;;
      '+'*)
        # Skip non-code files (markdown, allowlist, sweep script itself)
        _d7_is_nonprose "$CURRENT_FILE" && continue
        # Added line (not the +++ file header). It matched OUTBOUND_ERE OR a
        # bare triple-quote marker in the pre-filter above (DGN-1518 widened
        # the pre-filter for the latter -- see the comment there).
        content="${dline#+}"
        # Docstring continuation lines (DGN-1518): compute the new toggle
        # state from THIS line FIRST (even a line that gets skipped below by
        # _d7_is_comment, e.g. the opening/closing """ delimiter itself,
        # must still flip the state -- otherwise an interior line two lines
        # later would never learn it is inside a docstring). Skip if either
        # the incoming or the outgoing state was "inside a docstring".
        was_in_docstring=$DOCSTRING_STATE
        quote_marks="$(printf '%s' "$content" | grep -oE '"""|'"'''"'' 2>/dev/null | wc -l | tr -d ' ')"
        if [ $(( quote_marks % 2 )) -eq 1 ]; then
          DOCSTRING_STATE=$(( 1 - DOCSTRING_STATE ))
        fi
        # Skip comment lines -- inert prose inside a code file.
        _d7_is_comment "$content" && continue
        if [ "$was_in_docstring" = "1" ] || [ "$DOCSTRING_STATE" = "1" ]; then
          continue
        fi
        # The pre-filter also lets through triple-quote-only lines that carry
        # no outbound primitive at all -- confirm this line still matches
        # OUTBOUND_ERE before doing any more work on it.
        printf '%s' "$content" | grep -qE "$OUTBOUND_ERE" || continue
        # Inline trailing comment (DGN-1518): if the only OUTBOUND_ERE match
        # on this line lives after an unquoted '#'/'//', the match is inert
        # prose riding along a real code line, not a call.
        stripped="$(_d7_strip_trailing_comment "$content")"
        if ! printf '%s' "$stripped" | grep -qE "$OUTBOUND_ERE"; then
          continue
        fi
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

  # Load outbound allowlist (substring match against "file::content" entries).
  # _load_ob_allow appends parsed path-substrings from $1 onto OB_ALLOW; used
  # for both the scanned repo's own file and the optional reviewer-workspace
  # extra file (DGN-1361) so the two sources share identical parsing.
  OB_ALLOW=""
  _load_ob_allow() {
    local file="$1" aline path_part
    [ -f "$file" ] || return 0
    while IFS= read -r aline; do
      aline="${aline%%#*}"        # strip comment
      # strip leading/trailing whitespace
      aline="$(printf '%s' "$aline" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      # allowlist entries have format: <path-substring> | <reason> | <ref> | <date>
      # extract the path-substring (first field before |)
      path_part="${aline%%|*}"
      path_part="$(printf '%s' "$path_part" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      [ -n "$path_part" ] && OB_ALLOW="${OB_ALLOW}${path_part}"$'\n'
    done < "$file"
  }
  _load_ob_allow "$OUTBOUND_ALLOWLIST"
  if [ -n "${D7_OUTBOUND_ALLOWLIST_EXTRA:-}" ]; then
    case "$D7_OUTBOUND_ALLOWLIST_EXTRA" in
      /*)
        if [ -f "$D7_OUTBOUND_ALLOWLIST_EXTRA" ]; then
          _load_ob_allow "$D7_OUTBOUND_ALLOWLIST_EXTRA"
        else
          echo "note: D7_OUTBOUND_ALLOWLIST_EXTRA set but not found: $D7_OUTBOUND_ALLOWLIST_EXTRA (ignored; entries in it will NOT clear hits)"
        fi
        ;;
      *)
        echo "note: D7_OUTBOUND_ALLOWLIST_EXTRA must be an absolute path, got: $D7_OUTBOUND_ALLOWLIST_EXTRA (ignored)"
        ;;
    esac
  fi

  # DGN-1860 shipped fragments -> OB_ALLOW_EXACT (exact repo-relative path).
  # Same field parsing as _load_ob_allow; a leading './' is dropped. A kit
  # entry outside the kit delivery surfaces is ignored with a note: a kit
  # may vouch for its own payload, never for framework or owner files.
  OB_ALLOW_EXACT=""
  _load_ob_allow_exact() {
    local file="$1" layer="$2" aline path_part
    [ -f "$file" ] || return 0
    while IFS= read -r aline; do
      aline="${aline%%#*}"
      case "$aline" in *'|'*) : ;; *) continue ;; esac
      path_part="${aline%%|*}"
      path_part="$(printf '%s' "$path_part" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      path_part="${path_part#./}"
      [ -z "$path_part" ] && continue
      if [ "$layer" = "kit" ]; then
        case "$path_part" in
          *..*) echo "note: kit fragment ${file##*/}: entry ignored (path has '..'): $path_part"; continue ;;
          .claude/skills-bundle/?*|routines/bundle/?*|service/?*|database/?*|knowledge/?*) : ;;
          *) echo "note: kit fragment ${file##*/}: entry ignored (not a kit delivery surface): $path_part"; continue ;;
        esac
      fi
      OB_ALLOW_EXACT="${OB_ALLOW_EXACT}${path_part}"$'\n'
    done < "$file"
  }
  _load_ob_allow_exact "$OB_FW_FRAGMENT" framework
  while IFS= read -r _obk; do
    [ -n "$_obk" ] && _load_ob_allow_exact "$_obk" kit
  done <<EOF
$OB_KIT_FRAGMENTS
EOF

  # Filter hits through allowlist (substring match on file path for the
  # owner/extra layers; exact path for the shipped fragments)
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
    if [ "$matched" = "0" ] && [ -n "$OB_ALLOW_EXACT" ]; then
      while IFS= read -r pat; do
        [ -z "$pat" ] && continue
        if [ "$file_part" = "$pat" ]; then matched=1; break; fi
      done <<PATEOF
$OB_ALLOW_EXACT
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
    TICKET_FILE=""
    case "$D7_REVIEW_TICKET" in
      /*)
        # Absolute (DGN-1361): reviewer-workspace path, used as-is -- but
        # restricted to *.md so it cannot name arbitrary non-ticket files
        # (e.g. this script itself; see the header comment above).
        if [ "${D7_REVIEW_TICKET%.md}" != "$D7_REVIEW_TICKET" ]; then
          TICKET_FILE="$D7_REVIEW_TICKET"
        else
          echo "D7-REVIEW-ERROR: absolute D7_REVIEW_TICKET must be a .md file: $D7_REVIEW_TICKET"
          echo "(push still blocked)"
        fi
        ;;
      *)
        TICKET_FILE="$REPO_ROOT/worklog/${D7_REVIEW_TICKET}"
        ;;
    esac
    if [ -n "$TICKET_FILE" ]; then
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

# ---- staged added-lines mode (DGN-1708 pre-commit gate) --------------------
# Reuse the D7 discipline: one checked, materialized Git diff is the only
# source of truth.  Unlike --staged, this mode never reads an entire staged
# file, so an old secret outside the newly-added hunk cannot block a commit
# that does not introduce it.  The ordinary category scanner below remains the
# sole secret detector: this block only reconstructs added lines into private
# scratch files, retaining their repository-relative paths for hit reporting.
SCAN_CONTENT_ROOT="$REPO_ROOT"
STAGED_ADDED_FILES=""
STAGED_ADDED_ROOT=""
if [ "$MODE" = "staged-added-lines" ]; then
  STAGED_ADDED_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sweep-staged-added.XXXXXX" 2>/dev/null)"
  if [ -z "$STAGED_ADDED_ROOT" ] || [ ! -d "$STAGED_ADDED_ROOT" ]; then
    echo "ERROR: cannot create temporary directory for staged added-lines scan" >&2
    exit 2
  fi
  _staged_added_cleanup() { rm -rf "$STAGED_ADDED_ROOT"; }
  trap _staged_added_cleanup EXIT
  STAGED_DIFF="$STAGED_ADDED_ROOT/diff"
  if ! git -C "$REPO_ROOT" diff --cached --no-ext-diff --no-textconv --no-color \
        --src-prefix=a/ --dst-prefix=b/ --unified=0 -- > "$STAGED_DIFF" 2>/dev/null; then
    echo "ERROR: cannot read staged diff for added-lines scan" >&2
    exit 2
  fi
  CURRENT_FILE=""
  while IFS= read -r dline; do
    case "$dline" in
      '+++ b/'*)
        CURRENT_FILE="${dline#+++ b/}"
        # Git pathnames cannot contain NUL, but reject traversal defensively:
        # this is a scratch materializer, never a pathname normalizer.
        case "$CURRENT_FILE" in ''|/*|../*|*/../*|.. ) CURRENT_FILE="" ;; esac
        ;;
      '+'*)
        [ "$dline" = '+++' ] && continue
        [ -n "$CURRENT_FILE" ] || continue
        target="$STAGED_ADDED_ROOT/files/$CURRENT_FILE"
        mkdir -p "$(dirname "$target")" || { echo "ERROR: cannot materialize staged added-lines scan" >&2; exit 2; }
        printf '%s\n' "${dline#+}" >> "$target"
        if ! printf '%s' "$STAGED_ADDED_FILES" | grep -qxF "$CURRENT_FILE"; then
          STAGED_ADDED_FILES="${STAGED_ADDED_FILES}${CURRENT_FILE}"$'\n'
        fi
        ;;
    esac
  done < "$STAGED_DIFF"
  SCAN_CONTENT_ROOT="$STAGED_ADDED_ROOT/files"
fi

# ---- private owner-backup exemption (DGN-808) -------------------------------
# Declarative per-repo marker, no central registry: a private single-owner
# backup repo self-declares via `.dogany-private-backup` at the repo root.
# Valid declaration -> owner-PII cats {2,3,4,5,9,10} skipped (cat10 since
# DGN-1696); real-secret cats {1,6,7,8} always enforced. Every failure direction is CLOSED:
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
# DGN-1696: marker SOURCE. --ref -> the swept commit's tree (an uncommitted
# marker cannot exempt a push); otherwise the working tree as before.
# PB_KIND: "" absent | symlink | file | other (directory / unreadable).
PB_KIND=""
PB_FIRST=""
_pb_first_decl() {  # stdin: marker content -> first non-comment line, trimmed
  grep -vE '^[[:space:]]*(#|$)' 2>/dev/null | head -1 \
    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}
if [ -n "$SWEEP_COMMIT" ]; then
  PB_ENTRY="$(git -C "$REPO_ROOT" ls-tree --full-tree "$SWEEP_COMMIT" -- .dogany-private-backup 2>/dev/null)"
  if [ -n "$PB_ENTRY" ]; then
    case "$PB_ENTRY" in
      120000\ *)   PB_KIND="symlink" ;;
      *\ blob\ *) PB_KIND="file"; PB_FIRST="$(_ref_blob .dogany-private-backup | _pb_first_decl)" ;;
      *)           PB_KIND="other" ;;
    esac
  elif [ -e "$PB_MARKER" ] || [ -L "$PB_MARKER" ]; then
    {
      echo "secret-sweep: WARNING -- .dogany-private-backup exists in the working tree"
      echo "  but NOT in the swept commit ($SWEEP_COMMIT). Uncommitted markers never"
      echo "  exempt a push -- FULL sweep (DGN-1696). Commit the marker to declare."
    } >&2
  fi
elif [ -e "$PB_MARKER" ]; then
  # Defect3 (hardening): symlink as marker -> fail-closed (cannot be an owned
  # regular file if it is a symlink; could point outside the repo boundary).
  if [ -L "$PB_MARKER" ]; then
    PB_KIND="symlink"
  elif [ -f "$PB_MARKER" ] && [ -r "$PB_MARKER" ]; then
    PB_KIND="file"; PB_FIRST="$(_pb_first_decl < "$PB_MARKER")"
  else
    PB_KIND="other"
  fi
fi
if [ -n "$PB_KIND" ]; then
  if [ "$PB_KIND" = "symlink" ]; then
    {
      echo "secret-sweep: WARNING -- .dogany-private-backup is a symlink (must be a"
      echo "  regular file). Exemption REFUSED -- FULL sweep (fail-closed)."
    } >&2
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
    # exemption activate on a repo that has a public-export path. DGN-1696: under
    # --ref the swept commit's tree is checked too (every source only ever ADDS
    # a refusal).
    PB_PUBLISH_PRESENT=0
    if [ -e "$REPO_ROOT/scripts/publish.sh" ]; then
      PB_PUBLISH_PRESENT=1
    elif git -C "$REPO_ROOT" ls-files --error-unmatch scripts/publish.sh >/dev/null 2>&1; then
      PB_PUBLISH_PRESENT=1
    elif [ -n "$SWEEP_COMMIT" ] \
         && [ -n "$(git -C "$REPO_ROOT" ls-tree --full-tree "$SWEEP_COMMIT" -- scripts/publish.sh 2>/dev/null)" ]; then
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
      echo "  owner-PII cats {2,3,4,5,9,10} skipped; cats {1,6,7,8} still enforced (DGN-808, cat10 DGN-1696)"
    fi
  fi
fi

# ---- file list (bash 3.2: newline-delimited, no mapfile) --------------------
if [ "$MODE" = "staged" ]; then
  # staged, added/copied/modified only (skip deletions)
  FILE_LIST="$(git -C "$REPO_ROOT" diff --cached --name-only --diff-filter=ACM)"
elif [ "$MODE" = "staged-added-lines" ]; then
  FILE_LIST="${STAGED_ADDED_FILES%$'\n'}"
elif [ -n "$SWEEP_COMMIT" ]; then
  # DGN-1696: the swept commit's tree, not the index/working tree.
  FILE_LIST="$(git -C "$REPO_ROOT" ls-tree -r --full-tree --name-only "$SWEEP_COMMIT")"
else
  FILE_LIST="$(git -C "$REPO_ROOT" ls-files)"
fi
FILE_COUNT=0
[ -n "$FILE_LIST" ] && FILE_COUNT="$(printf '%s\n' "$FILE_LIST" | grep -c .)"

# ---- allowlist --------------------------------------------------------------
# DGN-1696: under --ref the allowlist is the swept commit's .sweepignore -- an
# uncommitted allowlist edit cannot clear a pushed hit.
ALLOW=""
SWEEPIGNORE_TEXT=""
if [ -n "$SWEEP_COMMIT" ]; then
  SWEEPIGNORE_TEXT="$(_ref_blob .sweepignore)"
elif [ -f "$REPO_ROOT/.sweepignore" ]; then
  SWEEPIGNORE_TEXT="$(cat "$REPO_ROOT/.sweepignore")"
fi
if [ -n "$SWEEPIGNORE_TEXT" ]; then
  while IFS= read -r line; do
    line="${line%%#*}"                       # strip comment
    line="$(echo "$line" | tr -d '[:space:]')"
    [ -n "$line" ] && ALLOW="${ALLOW}${line}"$'\n'
  done <<EOF
$SWEEPIGNORE_TEXT
EOF
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
# cat10: private-network literals. IPCORE is the address body shared by the
# detect pattern (cat10 below) and its CIDR-notation strip filter (DGN-1276
# M3): "100.64.0.0/10" describing the range must not itself block, only a
# live literal address does.
IPCORE10='100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}'
CAT_NAME[10]="private-network-literal"
CAT_PAT[10]="\\b(${IPCORE10})\\b|\\b[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\\.ts\\.net\\b"

# ---- scan (CAT_COUNT is indexed 1-10, fine in bash 3.2) ---------------------
CAT_COUNT=(0 0 0 0 0 0 0 0 0 0 0)   # indices 1..10 used
TOTAL=0
HITLINES=""

record() {  # $1=cat idx  $2=file  $3=detail
  CAT_COUNT[$1]=$(( ${CAT_COUNT[$1]:-0} + 1 ))
  TOTAL=$(( TOTAL + 1 ))
  HITLINES="${HITLINES}  [cat${1} ${CAT_NAME[$1]}] ${2}${3:+ :: }${3}"$'\n'
}

# DGN-1696: under --ref each blob is materialized into one private temp file
# (raw git object -- no smudge/textconv) and scanned there instead of disk.
REF_BLOB_FILE=""
if [ -n "$SWEEP_COMMIT" ]; then
  REF_BLOB_FILE="$(mktemp "${TMPDIR:-/tmp}/sweep-blob.XXXXXX" 2>/dev/null)"
  if [ -z "$REF_BLOB_FILE" ] || [ ! -f "$REF_BLOB_FILE" ]; then
    echo "ERROR: cannot create temp file for --ref content scan" >&2
    exit 2
  fi
  trap 'rm -f "$REF_BLOB_FILE"' EXIT
fi

while IFS= read -r f; do
  [ -z "$f" ] && continue
  if is_allowed "$f"; then continue; fi
  abs="$SCAN_CONTENT_ROOT/$f"
  if [ -n "$REF_BLOB_FILE" ]; then
    abs="$REF_BLOB_FILE"
    # not a blob (gitlink) -> nothing to scan; cat8 names below still apply
    _ref_blob "$f" > "$REF_BLOB_FILE" || : > "$REF_BLOB_FILE"
  fi

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

  for i in 1 2 3 4 5 6 7 10; do
    # Private owner-backup exemption (DGN-808): owner-PII content cats
    # {2-5,10} skipped ONLY under a valid declaration (cat10 DGN-1696);
    # {1,6,7} always run.
    if [ "$PRIVATE_BACKUP_EXEMPT" = "1" ]; then
      case "$i" in 2|3|4|5|10) continue ;; esac
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
    if [ "$i" = "10" ]; then
      # TEST-ONLY- carries the same form-based fixture contract as cat7.
      # A fake tailnet hostname or private address must declare itself on the
      # value line; a path allowlist would let a later real infrastructure
      # literal slip into that fixture file.
      m="$(printf '%s\n' "$m" | grep -Ev 'TEST-ONLY-')"
      [ -z "$m" ] && continue
      # cat10 CIDR-notation exemption (DGN-1276 M3): a range being DESCRIBED
      # ("100.64.0.0/10", "10.0.0.0/8" ...) is not a live literal address.
      # Strip every "<address>/<prefix-len>" span from each matched line and
      # re-check -- a line carrying BOTH a range description and a real
      # literal (or a bare literal with no CIDR suffix at all) still blocks.
      m="$(printf '%s\n' "$m" | sed -E "s#(${IPCORE10})/[0-9]{1,2}##g" \
        | grep -E "${CAT_PAT[$i]}")"
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
#   (b) allow-file, resolved in the SAME two-tier order as the owner-identity
#       file above (:444-447 -- allowhome, DGN-1104): "$REPO_ROOT/.sweep-identity-allow"
#       (repo-local override, checked first) else "$HOME/.dogany/sweep-identity-allow"
#       (machine-global default). Entries, one per line:
#         Name <email> | reason | ref | date
#       Entries without a '| reason' field are IGNORED (documentation is
#       mandatory). Keep this file UNTRACKED: it has no publish owner so it
#       cannot ship through the curated export, and if it were ever tracked
#       its own content trips cat4 (owner-name) until deliberately reviewed
#       into .sweepignore -- a forced review point. Rooting the default copy
#       at $HOME (same root the detection pattern already loads from) is what
#       lets the allowlist travel with a fresh clone/worktree on the OWNER's
#       own machine, instead of being re-litigated per checkout.
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
PUBLISH_IDENT_FOUND=0
if [ -f "$PUBLISH_SH" ]; then
  SNAP_NAME="$(sed -n 's/^SNAP_NAME="\(.*\)"[[:space:]]*$/\1/p' "$PUBLISH_SH" | head -1)"
  SNAP_EMAIL="$(sed -n 's/^SNAP_EMAIL="\(.*\)"[[:space:]]*$/\1/p' "$PUBLISH_SH" | head -1)"
  if [ -n "$SNAP_NAME" ] && [ -n "$SNAP_EMAIL" ]; then
    IDENT_ALLOW="${IDENT_ALLOW}${SNAP_NAME} <${SNAP_EMAIL}>"$'\n'
    PUBLISH_IDENT_FOUND=1
  fi
fi
# Two-tier resolution (allowhome, DGN-1104): repo-local override first, else
# the machine-global default -- same order/root as SWEEP_IDENT_FILE above
# (:444-447), so the allow-file travels with the detection pattern instead of
# being pinned to one checkout.
IDENT_ALLOW_FILE=""
if [ -f "$REPO_ROOT/.sweep-identity-allow" ]; then
  IDENT_ALLOW_FILE="$REPO_ROOT/.sweep-identity-allow"
elif [ -f "$HOME/.dogany/sweep-identity-allow" ]; then
  IDENT_ALLOW_FILE="$HOME/.dogany/sweep-identity-allow"
fi
ALLOWFILE_PRESENT=0
ALLOWFILE_ENTRY_COUNT=0
if [ -n "$IDENT_ALLOW_FILE" ]; then
  ALLOWFILE_PRESENT=1
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
    if [ -n "$ident" ]; then
      IDENT_ALLOW="${IDENT_ALLOW}${ident}"$'\n'
      ALLOWFILE_ENTRY_COUNT=$(( ALLOWFILE_ENTRY_COUNT + 1 ))
    fi
  done < "$IDENT_ALLOW_FILE"
fi

# cat9 allow-input visibility (DGN-1064 follow-up, updated for allowhome
# DGN-1104): the allow-file is UNTRACKED by design (see the cat9 notes above)
# and is now resolved two-tier ($REPO_ROOT override, else $HOME/.dogany
# default -- same root as the detection pattern), so it travels with any
# clone/worktree on the OWNER's own machine once the default copy exists at
# $HOME/.dogany/sweep-identity-allow. A repo-root override still lets one
# checkout diverge (verdict differently from its siblings) on purpose; that
# divergence must never be silent, so always report which cat9 allow sources
# were found/absent (counts and paths only -- never the allowlisted identity
# strings themselves, which would leak the same owner PII this gate exists to
# keep out of logs).
if [ "$PUBLISH_IDENT_FOUND" = "1" ]; then
  PUBLISH_IDENT_NOTE="FOUND (1 identity parsed live from scripts/publish.sh)"
else
  PUBLISH_IDENT_NOTE="ABSENT (no scripts/publish.sh here, or SNAP_NAME/SNAP_EMAIL unset)"
fi
if [ "$ALLOWFILE_PRESENT" = "1" ]; then
  ALLOWFILE_NOTE="FOUND ($ALLOWFILE_ENTRY_COUNT entries) at $IDENT_ALLOW_FILE"
else
  ALLOWFILE_NOTE="ABSENT (checked $REPO_ROOT/.sweep-identity-allow then $HOME/.dogany/sweep-identity-allow)"
fi
echo "secret-sweep :: cat9-allow-inputs: publish-identity=$PUBLISH_IDENT_NOTE"
echo "secret-sweep :: cat9-allow-inputs: allow-file=$ALLOWFILE_NOTE"
if [ "$ALLOWFILE_PRESENT" != "1" ]; then
  echo "secret-sweep :: cat9-allow-inputs: NOTE -- allow-file is untracked by design (repo-root override does not travel with clones/worktrees); neither the repo-root override nor the \$HOME/.dogany default was found here, so cat9 is judged MORE STRICTLY in this checkout than in one with either file present."
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
if [ "$PRIVATE_BACKUP_EXEMPT" != "1" ] && [ "$MODE" != "staged-added-lines" ]; then
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
  SCOPE_SUFFIX=" [reduced sweep: cats 1,6,7,8 enforced; owner-PII cats 2,3,4,5,9,10 SKIPPED -- DGN-808/DGN-1696]"
fi
echo "secret-sweep :: repo=$REPO_ROOT mode=$MODE${SWEEP_COMMIT:+ ref=$SWEEP_COMMIT} files=$FILE_COUNT"
if [ "$IDENT_ALLOWED_COUNT" -gt 0 ]; then
  echo "note: cat9 allowlisted commit-identity line(s) skipped: $IDENT_ALLOWED_COUNT"
fi
if [ "$TOTAL" -eq 0 ]; then
  echo "RESULT: CLEAN (0 hits)${SCOPE_SUFFIX}"
  exit 0
fi

echo "RESULT: DIRTY ($TOTAL hit(s))${SCOPE_SUFFIX}"
echo "-- category summary --"
for i in 1 2 3 4 5 6 7 8 9 10; do
  c="${CAT_COUNT[$i]:-0}"
  [ "$c" -gt 0 ] && printf "  cat%s %-24s %d\n" "$i" "${CAT_NAME[$i]}" "$c"
done
echo "-- hits --"
printf "%s" "$HITLINES"
echo "STOP: do not push until clean (or allowlist legit placeholders in .sweepignore)."
exit 1
