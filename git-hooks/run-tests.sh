#!/usr/bin/env bash
# run-tests.sh -- estate test lane runner (DGN-1211).
#
# The hook harness (core.hooksPath=git-hooks) existed estate-wide, but NOTHING
# in it ever ran a test: bridge pytest, pack tests, and the tests/ lints all
# had zero readers (DGN-1209 grill + estate-wide reader census). This runner is the single
# mechanism that gives them one. It is invoked by git-hooks/pre-push (the
# content-gate hook, alongside secret-sweep and D7); pre-commit stays a pure
# git-topology guard (detached HEAD / trunk) by existing doctrine.
#
# SINGLE VARIANT (DGN-1058 doctrine): git-hooks/run-tests.sh and
# agents/.template/git-hooks/run-tests.sh must be byte-identical -- update.sh
# 3k1 delivers the ROOT copy to live instances while mint.sh copies the
# TEMPLATE, so a divergence silently weakens freshly minted instances. This
# runner CHECKS that parity itself on every run where both copies exist
# (canonical checkouts), so the invariant has a reader instead of a comment.
#
# Discovery is runtime + repo-content-driven (same pattern as the pre-push
# missing-sweep policy): no repo-type flags, the repo's own tree decides.
#   SUITE class   every file in the tree that DECLARES a lane -- see the
#                 DGN-1267 discovery note below. Discovery is NAME-BLIND
#                 (DGN-1263) and now PATH-BLIND too (DGN-1267). It used to be
#                 the glob
#                 tests/*_lint.{py,sh}, which made the FILENAME the gate:
#                 tests/*_selftest.sh -- 29 suites, 4 of them red -- had zero
#                 readers, and two regressions that WANTED a reader had to
#                 impersonate the lint name to get one (see the header of
#                 tests/dgn530_bundle_clobber_guard_lint.sh:4-8, which says so
#                 outright). A convention authors must impersonate is a trap,
#                 not a convention. Every discovered file must DECLARE its
#                 lane in its own first 60 lines:
#                     # test-lane: fast          -- runs on every push
#                     # test-lane: slow          -- runs in the slow lane
#                     # test-lane: manual <why>  -- never auto-run; <why> is
#                                                  printed on EVERY run
#                     # test-lane: lib <why>     -- not a test (shared helper)
#                 No marker -> UNLANED -> the run FAILS. Renaming a file
#                 changes nothing; only the declaration does.
#   PYTEST class  a directory NAMED tests/ holding >=1 direct test_*.py and
#                 no lane-declaring file -- structural, not a path list
#                 (DGN-1267). This covers agents/.template/bridge/tests,
#                 agents/.template/memory-engine/tests, a live instance's
#                 bridge/tests and packs/*/tests without naming any of them.
#
# DISCOVERY (DGN-1267). The path list WAS the bug. Three tickets in a row were
# the same shape -- DGN-1211 "the tests have no reader" (runner created),
# DGN-1263 "the NAME glob is too narrow" (replaced by a lane declaration),
# DGN-1267 "the PATHS the declaration covers are too narrow". <root>/tests/
# plus bridge/tests plus packs/*/tests missed 80 live suite files: 37 in
# agents/.template/routines/tests (where the DGN-675 branch put its regression),
# 41 in scripts/tests, 1 in agents/.template/scripts/tests, 1 in
# docs/proposals/dgn-1120/tests -- and one whole pytest root,
# agents/.template/memory-engine/tests (73 tests, measured green 6.6s).
# Adding a fourth path would have booked the fifth iteration, so discovery is
# now two RULES, no enumeration:
#   RUN axis      every file anywhere in the tree whose first 60 lines carry a
#                 '# test-lane:' marker. Path-blind: a suite gets a reader by
#                 DECLARING, wherever it lives. Measured cost: one grep pass
#                 over 434 candidate .sh/.py files, 0.2s.
#   UNLANED axis  a declaration cannot detect a file that declares NOTHING, so
#                 "who forgot to declare" stays structural: every directory
#                 literally named tests/ (minus pytest roots) must have a
#                 marker on every .sh/.py under it. A new foo/tests/ is covered
#                 the day it is created, with no edit here.
# STATED HOLE: the UNLANED axis reaches tests/ DIRECTORIES only. A test-shaped
# file parked outside one is caught by the RUN axis if it declares, and by
# NOTHING if it does not. A 2026-09-04 name census (git ls-files, test-shaped
# names, outside every tests/ dir) found exactly two such suites -- both were
# laned by DGN-1267: the crew-merge suite under scripts/crew-merge/ and
# service/mailer/selftest.py. The census also turned up two files that LOOK
# like suites and are not (agents/.template/routines/anchor_crossref_lint.py is
# a production lint; scripts/pack/knowledge_selftest.sh is a production gate
# pack_install.sh runs as STEP 7c) -- neither is laned, on purpose. Closing
# this hole by rule would mean guessing test-ness from a filename, which is the
# exact DGN-1263 trap, so it is a HOLE ON RECORD, re-checked by census, not a
# rule pretending to be complete.
# Excluded on both axes: any path with a fixtures/ segment (payload data, not
# suites -- scripts/tests carries 133 such files, one of them literally named
# test_engine.py), __pycache__, and anything git ignores (.worktrees/).
# Declared edge (grilled 2026-09-04, case G8): a tests/ dir holding BOTH a
# lane-declaring file AND bare test_*.py is SUITE class, so those test_*.py go
# UNLANED and BLOCK. That is deliberate -- the alternative is guessing which
# half of a mixed directory pytest owns, and guessing wrong there is silent.
# The failure names the exact file and the four lanes, so the author is told
# what to do; a mixed root just has to say which files are which.
#
# LANES (DGN-1263). One pre-push lane cannot hold both. Measured 2026-09-04,
# after DGN-1267 widened discovery: 90 fast suites + 2 pytest roots = 145s
# wall, 44 slow suites, 4 manual, 1 lib. DECLARED COST: the fast lane roughly
# doubled (it was ~60s over 41 fast suites + 1 pytest root before DGN-1267).
# That is the price of the 80 files that previously had NO reader at all, and
# it is stated here rather than absorbed silently; if 145s proves intolerable
# the answer is to move suites to the slow lane on measured times, not to
# narrow discovery again. Putting the SLOW ones on every push paralyses push;
# leaving them out of every lane is exactly today's disease. So:
#   fast lane  -- every push (pre-push).
#   slow lane  -- run by a STALENESS CLOCK, not by a promise: a stamp under
#                 the git common dir records the last successful slow run; a
#                 fast-lane invocation whose stamp is missing or older than
#                 DGN_SLOW_LANE_MAX_AGE_H (default 168h) ESCALATES ITSELF to
#                 the slow lane and says so. That is a machine reader that
#                 needs no scheduler and cannot be forgotten. Force either
#                 lane with DGN_TEST_LANE=fast|slow|all.
#   manual     -- never auto-run, but its reason is re-printed on every single
#                 run, so "we can't run this" never decays into silence.
# Declared trade-off: the stamp records the ATTEMPT, not the success, and
# carries the verdict (green/red). A RED slow lane therefore blocks the push
# that ran it and then stays deferred until the window expires -- one blocked
# push, not every push for a week. The alternative (re-run until green) costs
# the full lane on every single push while it is red. The deferred-red case
# is printed with the command to force a re-run, so it is a stated choice,
# not a hole.
#
# KNOWN-RED registry (tests/KNOWN-RED, DGN-1263): a suite listed there is
# still RUN; a failure is reported as RED-KNOWN and does NOT block, and a
# PASS is a FAILURE ("stale quarantine -- delist it"). Quarantine can only
# shrink, and its size is printed on every run.
#
# Verdict policy (every branch is deliberate -- DGN-1154 / DGN-1211):
#   TEST FAILURE            exit 1  (push blocked)
#   TIMEOUT                 exit 1  -- a hung suite is indistinguishable from
#                           a broken one, so timeout = FAILURE by design.
#                           Budgets: 60s per lint (measured max 0.32s),
#                           300s per pytest root (measured 24-27s).
#   QUIET-GREEN             exit 1  -- a discovered pytest root that collects
#                           nothing, or passes 0 tests while skipping the
#                           rest, "succeeded" without testing anything. That
#                           is the exact disease this runner exists to cure
#                           (fixture-absent skip -> green, DGN-1154), so it
#                           blocks instead of passing.
#   PARITY BREAK            exit 1  -- root vs template single-variant
#                           divergence (pre-push / run-tests.sh) on a repo
#                           carrying both copies.
#   RUNNER DEGRADED         exit 0, LOUD -- python3/pytest missing is a
#                           dev-setup gap, not a test failure; same
#                           fail-open-with-printed-note direction as the
#                           missing-sweep policy on non-PII repos (DGN-960
#                           B5). Never printed as PASS.
#   NO ASSETS AT ALL        exit 0, LOUD -- public/stripped clones carry no
#                           test assets; walling their pushes would be a
#                           dev-setup wall. The zero itself is printed on
#                           every run: silence is never success here.
#
# A RESULT line is printed on EVERY invocation, including full success and
# zero-discovery (DGN-1058 precedent: the sweep prints its scope on every
# clean push). Bypass for a deliberate red push: git push --no-verify.

set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || dirname "$SELF_DIR")"

FAILS=0
DEGRADED=0
SUITE_FOUND=0      # test assets under tests/ that are not declared 'lib'
SUITE_RAN=0
SUITE_DEFER=0      # slow-lane suites skipped by a fast-lane run
SUITE_MANUAL=0     # never auto-run; reason re-printed every run
SUITE_LIB=0        # declared non-tests (shared fixtures/helpers)
SUITE_UNLANED=0    # no '# test-lane:' marker -> hard failure
RED_KNOWN=0        # listed in tests/KNOWN-RED and failed (non-blocking)
RED_STALE=0        # listed in tests/KNOWN-RED and PASSED (blocking)
PY_FOUND=0
PY_RAN=0

_note() { echo "[run-tests] $*" >&2; }

# ---- timeout wrapper --------------------------------------------------------
# macOS ships no timeout(1)/gtimeout(1) (measured on the estate host), so the
# budget is enforced via a python3 subprocess wrapper. rc 124 = timeout,
# rc 127 = target binary missing. python3 itself missing -> the caller treats
# the whole class as RUNNER DEGRADED before reaching here.
_timed() {
  python3 -c '
import subprocess, sys
t = int(sys.argv[1])
try:
    sys.exit(subprocess.run(sys.argv[2:], timeout=t).returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)
except FileNotFoundError:
    sys.exit(127)
' "$@"
}

HAVE_PY=1
command -v python3 >/dev/null 2>&1 || HAVE_PY=0

# ---- single-variant parity (root vs template, canonical checkouts only) -----
for _sv in pre-push run-tests.sh; do
  if [ -f "$ROOT/git-hooks/$_sv" ] && [ -f "$ROOT/agents/.template/git-hooks/$_sv" ]; then
    if ! cmp -s "$ROOT/git-hooks/$_sv" "$ROOT/agents/.template/git-hooks/$_sv"; then
      _note "FAIL: single-variant parity broken (DGN-1058): git-hooks/$_sv != agents/.template/git-hooks/$_sv"
      _note "      mint delivers the template copy, update.sh 3k1 delivers the root copy --"
      _note "      a divergence ships a weaker gate to newly minted instances. Re-sync them byte-identical."
      FAILS=$((FAILS + 1))
    fi
  fi
done

# ---- asset discovery (rules, not paths -- DGN-1267) -------------------------
_TMPD="$(mktemp -d "${TMPDIR:-/tmp}/run-tests-disc.XXXXXX")"
_CAND="$_TMPD/cand"; _MARKERS="$_TMPD/markers"; _TDIRS="$_TMPD/tdirs"
_PYROOTS="$_TMPD/pyroots"; _UNLANED="$_TMPD/unlaned"
trap 'rm -rf "$_TMPD"' EXIT

# Candidate set: every .sh/.py the repo owns. git ls-files is preferred because
# it applies .gitignore for free (the canonical checkout carries ~60 sibling
# clones under .worktrees/, which find would otherwise descend into); the find
# fallback exists for a non-git tree and prunes the same shapes by hand.
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "$ROOT" ls-files --cached --others --exclude-standard -- '*.sh' '*.py' 2>/dev/null
else
  ( cd "$ROOT" 2>/dev/null && find . -type d \
      \( -name .git -o -name .worktrees -o -name __pycache__ -o -name node_modules \) -prune \
      -o -type f \( -name '*.sh' -o -name '*.py' \) -print 2>/dev/null | sed 's|^\./||' )
fi | grep -vE '(^|/)fixtures/|(^|/)__pycache__/' | LC_ALL=C sort > "$_CAND"

# RUN axis: one batched grep over the candidates, then a per-hit re-read to
# enforce the 60-line bound (only the hits pay that cost, ~100 files).
: > "$_MARKERS"
if [ -s "$_CAND" ]; then
  ( cd "$ROOT" && tr '\n' '\0' < "$_CAND" \
      | xargs -0 grep -l -E '^#[[:space:]]*test-lane:' 2>/dev/null ) \
  | while IFS= read -r _p; do
      sed -n '1,60p' "$ROOT/$_p" | grep -qE '^#[[:space:]]*test-lane:' && printf '%s\n' "$_p"
    done | LC_ALL=C sort > "$_MARKERS"
fi

# UNLANED axis: directories literally named tests/ (first such segment wins, so
# a nested tests/ inside an already-claimed root does not double-count).
awk -F/ '{for(i=1;i<NF;i++) if($i=="tests"){p=$1; for(j=2;j<=i;j++) p=p"/"$j; print p; break}}' \
  "$_CAND" | LC_ALL=C sort -u > "$_TDIRS"

: > "$_PYROOTS"; : > "$_UNLANED"
while IFS= read -r _d; do
  [ -n "$_d" ] || continue
  # pytest root iff it holds a DIRECT test_*.py and nothing under it declares a
  # lane. tests/ itself holds test_*.py only in subdirs (tests/mirror) and every
  # one of them declares a lane, so it stays SUITE class on both counts.
  if ls "$ROOT/$_d" 2>/dev/null | grep -qE '^test_.*\.py$' \
     && ! grep -q "^$_d/" "$_MARKERS"; then
    printf '%s\n' "$_d" >> "$_PYROOTS"
    continue
  fi
  grep "^$_d/" "$_CAND" | while IFS= read -r _f; do
    grep -qxF "$_f" "$_MARKERS" || printf '%s\n' "$_f"
  done >> "$_UNLANED"
done < "$_TDIRS"

# ---- SUITE class (lane-declared, NAME-BLIND -- DGN-1263) --------------------
LANE="${DGN_TEST_LANE:-fast}"
case "$LANE" in
  fast|slow|all) ;;
  *) _note "NOTE: unknown DGN_TEST_LANE='$LANE' -- treating as 'fast'"; LANE=fast ;;
esac
SLOW_MAX_AGE_H="${DGN_SLOW_LANE_MAX_AGE_H:-168}"
KNOWN_RED="$ROOT/tests/KNOWN-RED"

# Budgets and the fast/slow CUT come from a measured full sweep of all 52
# test assets under tests/ (2026-09-03; the per-suite numbers live in each
# file's own marker comment, the summary in tests/KNOWN-RED's header).
# The cut is 3s, chosen on the data, not on taste:
#   * the measurement has a real gap there -- 2.8s then 5.2s;
#   * it keeps the fast lane at 32.9s over 34 suites, the same order as the
#     bridge pytest this hook already pays (~25s), instead of tripling the
#     push tax. The 13 slow suites total 400.8s -- a 6.7min lane nobody would
#     tolerate on every push, which is exactly why it gets a clock instead.
# Slowest measured: fast 2.8s, slow 94.3s. The budgets sit far above both so
# cold caches and a loaded host never trip them, while a hang still does.
FAST_BUDGET="${DGN_FAST_BUDGET:-30}"
SLOW_BUDGET="${DGN_SLOW_BUDGET:-600}"

# Stamp for the slow-lane staleness clock. --git-common-dir so all worktrees
# of one clone share one clock (they share the code the slow lane tests).
STAMP=""
_gcd="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
case "$_gcd" in
  "")  ;;
  /*)  STAMP="$_gcd/dogany-slow-lane.stamp" ;;
  *)   STAMP="$ROOT/$_gcd/dogany-slow-lane.stamp" ;;
esac

_age_hours() {  # <file> -> integer hours since mtime, or empty if unknowable
  [ "$HAVE_PY" -eq 1 ] || return 0
  python3 -c '
import os, sys, time
try:
    print(int((time.time() - os.path.getmtime(sys.argv[1])) // 3600))
except OSError:
    pass
' "$1" 2>/dev/null
}

# tests/KNOWN-RED line format:  <repo-rel-path> <TICKET> <YYYY-MM-DD expiry> <note>
# Echoes "<ticket> <expiry>" for a LIVE entry; nothing once the expiry passes.
# An expired entry stops shielding its suite, so quarantine cannot outlive its
# own deadline just because nobody looked -- the clock un-quarantines it and
# the red starts blocking. TODAY is read once, from date(1).
_TODAY="$(date -u +%Y-%m-%d 2>/dev/null || echo 9999-12-31)"
_known_red_entry() {  # <relpath> -> "<ticket> <expiry>", or empty
  [ -f "$KNOWN_RED" ] || return 0
  awk -v f="$1" -v today="$_TODAY" \
    '$0 !~ /^[[:space:]]*#/ && $1 == f { if ($3 >= today) print $2, $3; exit }' "$KNOWN_RED"
}
_known_red_expired() {  # <relpath> -> "<ticket> <expiry>" if the entry is PAST due
  [ -f "$KNOWN_RED" ] || return 0
  awk -v f="$1" -v today="$_TODAY" \
    '$0 !~ /^[[:space:]]*#/ && $1 == f { if ($3 < today) print $2, $3; exit }' "$KNOWN_RED"
}

# The slow lane's reader: a clock, not a promise. A fast-lane run whose stamp
# is missing or stale escalates ITSELF and says so. No scheduler to forget.
SLOW_ESCALATED=0
if { [ -s "$_MARKERS" ] || [ -s "$_UNLANED" ]; } && [ "$LANE" = "fast" ]; then
  if [ -z "$STAMP" ]; then
    _note "NOTE: no git dir -- slow-lane staleness clock unavailable, slow suites have NO reader here."
    _note "      run them explicitly: DGN_TEST_LANE=all bash git-hooks/run-tests.sh"
  elif [ ! -f "$STAMP" ]; then
    _note "SLOW LANE: never run in this clone -- escalating THIS run to lane=all (DGN-1263)."
    LANE=all; SLOW_ESCALATED=1
  else
    _sage="$(_age_hours "$STAMP")"
    _sverdict="$(head -1 "$STAMP" 2>/dev/null)"
    if [ -z "$_sage" ]; then
      _note "NOTE: slow-lane stamp age unreadable ($STAMP) -- escalating THIS run to lane=all."
      LANE=all; SLOW_ESCALATED=1
    elif [ "$_sage" -ge "$SLOW_MAX_AGE_H" ]; then
      _note "SLOW LANE: last run ${_sage}h ago (>= ${SLOW_MAX_AGE_H}h) -- escalating THIS run to lane=all."
      LANE=all; SLOW_ESCALATED=1
    else
      _note "slow lane: last run ${_sage}h ago, verdict=${_sverdict:-unknown} (< ${SLOW_MAX_AGE_H}h) -- slow suites deferred on this run."
      if [ "${_sverdict:-}" = "red" ]; then
        _note "      that last run was RED and it is still inside the window, so this push is NOT re-verifying it."
        _note "      force a re-run: DGN_TEST_LANE=all bash git-hooks/run-tests.sh"
      fi
    fi
  fi
fi

# Stamp the ATTEMPT before running, not the success after it. If a red slow
# lane left the clock stale, EVERY subsequent push would re-pay the whole
# 400s lane on top of already being blocked. The verdict is written into the
# stamp so a deferred push still says whether the last slow run was green.
if [ "$SLOW_ESCALATED" -eq 1 ] && [ -n "$STAMP" ]; then
  echo "attempted" > "$STAMP" 2>/dev/null || true
fi

# UNLANED files never reach the run loop -- they have no lane to run on. They
# are reported here, before it, so the disease is named even on a green tree.
while IFS= read -r _rel; do
  [ -n "$_rel" ] || continue
  SUITE_FOUND=$((SUITE_FOUND + 1)); SUITE_UNLANED=$((SUITE_UNLANED + 1))
  _note "FAIL: UNLANED -- no '# test-lane:' marker in the first 60 lines: $_rel"
  _note "      add exactly one of: fast | slow | manual <why> | lib <why>   (DGN-1263)"
  FAILS=$((FAILS + 1))
done < "$_UNLANED"

if [ -s "$_MARKERS" ]; then
  while IFS= read -r _rel; do
    [ -n "$_rel" ] || continue
    _f="$ROOT/$_rel"
    _mk="$(sed -n '1,60p' "$_f" | grep -m1 -E '^#[[:space:]]*test-lane:' || true)"
    _lane="$(printf '%s' "$_mk" | sed -E 's/^#[[:space:]]*test-lane:[[:space:]]*//; s/[[:space:]].*$//')"
    _why="$(printf '%s' "$_mk" | sed -E 's/^#[[:space:]]*test-lane:[[:space:]]*[^[:space:]]*[[:space:]]*//')"

    case "$_lane" in
      lib)
        SUITE_LIB=$((SUITE_LIB + 1))
        continue
        ;;
      manual)
        SUITE_FOUND=$((SUITE_FOUND + 1)); SUITE_MANUAL=$((SUITE_MANUAL + 1))
        _note "MANUAL (not run, ever -- reason re-printed every run): $_rel"
        _note "        reason: ${_why:-NONE GIVEN -- an unexplained manual lane is itself a defect}"
        continue
        ;;
      fast)
        SUITE_FOUND=$((SUITE_FOUND + 1))
        _budget="$FAST_BUDGET"
        ;;
      slow)
        SUITE_FOUND=$((SUITE_FOUND + 1))
        _budget="$SLOW_BUDGET"
        if [ "$LANE" = "fast" ]; then
          SUITE_DEFER=$((SUITE_DEFER + 1))
          continue
        fi
        ;;
      *)
        # Reached only if the marker line parses to an unknown word (the
        # marker-less case is caught by the UNLANED sweep above). Same verdict:
        # a lane nobody defined claims nothing.
        SUITE_FOUND=$((SUITE_FOUND + 1)); SUITE_UNLANED=$((SUITE_UNLANED + 1))
        _note "FAIL: UNKNOWN LANE '\''$_lane'\'' -- not one of fast|slow|manual|lib: $_rel"
        FAILS=$((FAILS + 1))
        continue
        ;;
    esac

    if [ "$HAVE_PY" -ne 1 ]; then
      case "$_f" in
        *.py)
          _note "DEGRADED: python3 missing -- suite NOT RUN (not a pass): $_rel"
          DEGRADED=$((DEGRADED + 1))
          continue
          ;;
      esac
    fi

    _sout="$(mktemp "${TMPDIR:-/tmp}/run-tests.XXXXXX")"
    case "$_f" in
      *.py) _timed "$_budget" python3 "$_f" >"$_sout" 2>&1; _rc=$? ;;
      *.sh)
        if [ "$HAVE_PY" -eq 1 ]; then
          _timed "$_budget" bash "$_f" >"$_sout" 2>&1; _rc=$?
        else
          # No python3 -> no timeout wrapper; run bare rather than not at all.
          bash "$_f" >"$_sout" 2>&1; _rc=$?
        fi
        ;;
    esac
    SUITE_RAN=$((SUITE_RAN + 1))
    _entry="$(_known_red_entry "$_rel")"
    _expired="$(_known_red_expired "$_rel")"
    [ -n "$_expired" ] && _note "NOTE: quarantine EXPIRED for $_rel ($_expired) -- it no longer shields this suite."

    if [ "$_rc" -eq 0 ]; then
      if [ -n "$_entry" ]; then
        # Passing while quarantined is reported, not punished: a green run can
        # be environment luck (measured: dgn907 is red only when PYTHONPATH
        # leaks a live instance). The EXPIRY, not this line, is what stops the
        # registry from rotting.
        _note "STALE QUARANTINE: $_rel PASSED but is still listed in tests/KNOWN-RED ($_entry) -- delist it."
        RED_STALE=$((RED_STALE + 1))
      fi
    elif [ -n "$_entry" ]; then
      _note "RED-KNOWN (quarantined until the listed date, does NOT block): $_rel rc=$_rc -- $_entry"
      tail -5 "$_sout" >&2
      RED_KNOWN=$((RED_KNOWN + 1))
    elif [ "$_rc" -eq 124 ]; then
      _note "FAIL: suite TIMEOUT (>${_budget}s -- timeout = failure by design): $_rel"
      FAILS=$((FAILS + 1))
    else
      _note "FAIL: suite rc=$_rc: $_rel"
      tail -20 "$_sout" >&2
      FAILS=$((FAILS + 1))
    fi
    rm -f "$_sout"
  done < "$_MARKERS"

  # Record the verdict of this slow run into the clock.
  if [ "$LANE" != "fast" ] && [ -n "$STAMP" ]; then
    if [ "$FAILS" -eq 0 ]; then
      echo "green" > "$STAMP" 2>/dev/null \
        && _note "slow lane green -- staleness clock reset ($STAMP)"
    else
      echo "red" > "$STAMP" 2>/dev/null \
        && _note "slow lane RED -- clock recorded verdict=red ($STAMP); this push is blocked by the failures above."
    fi
  fi
fi

# ---- PYTEST class -----------------------------------------------------------
# Structural, from the discovery pass above -- no path list to keep in sync
# with the tree (DGN-1267). agents/.template/memory-engine/tests was invisible
# to the old three-entry list and is picked up by this rule for free.
PY_ROOTS=()
while IFS= read -r _pr; do
  [ -n "$_pr" ] && PY_ROOTS+=("$ROOT/$_pr")
done < "$_PYROOTS"

if [ "${#PY_ROOTS[@]}" -gt 0 ]; then
  PY_FOUND=${#PY_ROOTS[@]}
  if [ "$HAVE_PY" -ne 1 ]; then
    _note "DEGRADED: python3 missing -- $PY_FOUND pytest root(s) NOT RUN (not a pass)"
    DEGRADED=$((DEGRADED + PY_FOUND))
  elif ! python3 -m pytest --version >/dev/null 2>&1; then
    _note "DEGRADED: pytest missing -- $PY_FOUND pytest root(s) NOT RUN (not a pass)"
    _note "          install: python3 -m pip install pytest"
    DEGRADED=$((DEGRADED + PY_FOUND))
  else
    for _root in "${PY_ROOTS[@]}"; do
      _rel="${_root#"$ROOT"/}"
      _out="$(mktemp "${TMPDIR:-/tmp}/run-tests.XXXXXX")"
      _timed 300 python3 -m pytest "$_root" -q -rs >"$_out" 2>&1
      _rc=$?
      PY_RAN=$((PY_RAN + 1))
      # Final pytest summary line ("N failed, M passed, ... in Xs").
      _sum="$(grep -E '^[0-9]+ .*(passed|failed|skipped|error|no tests ran)' "$_out" | tail -1)"
      [ -z "$_sum" ] && _sum="$(tail -1 "$_out")"
      if [ "$_rc" -eq 124 ]; then
        _note "FAIL: pytest TIMEOUT (>300s -- timeout = failure by design): $_rel"
        FAILS=$((FAILS + 1))
      elif [ "$_rc" -eq 5 ]; then
        _note "FAIL: QUIET-GREEN -- pytest root discovered but 0 tests collected: $_rel"
        _note "      an empty suite passing silently is the DGN-1154 disease; add tests or remove the dir."
        FAILS=$((FAILS + 1))
      elif [ "$_rc" -ne 0 ]; then
        _note "FAIL: pytest rc=$_rc: $_rel -- $_sum"
        tail -40 "$_out" >&2
        FAILS=$((FAILS + 1))
      elif ! printf '%s' "$_sum" | grep -qE '[0-9]+ passed'; then
        _note "FAIL: QUIET-GREEN -- pytest exited 0 with no passing test in $_rel ($_sum)"
        _note "      skip is NOT counted as pass (DGN-1154 / DGN-1211)."
        FAILS=$((FAILS + 1))
      else
        _note "$_rel: $_sum"
        # Partial skips pass, but never silently (DGN-1154).
        if printf '%s' "$_sum" | grep -qE '[0-9]+ skipped'; then
          _note "NOTE: skipped tests present in $_rel -- skips are surfaced, not counted as pass."
        fi
      fi
      rm -f "$_out"
    done
  fi
fi

# ---- verdict (a RESULT line prints on EVERY path -- silence is never success)
_result="lane=$LANE suites=$SUITE_RAN/$SUITE_FOUND deferred-slow=$SUITE_DEFER manual=$SUITE_MANUAL"
_result="$_result unlaned=$SUITE_UNLANED known-red=$RED_KNOWN stale-quarantine=$RED_STALE lib=$SUITE_LIB"
_result="$_result pytest-roots=$PY_RAN/$PY_FOUND failures=$FAILS degraded=$DEGRADED"
if [ "$FAILS" -gt 0 ]; then
  _note "RESULT: FAIL -- $_result"
  exit 1
fi
if [ "$SUITE_FOUND" -eq 0 ] && [ "$PY_FOUND" -eq 0 ]; then
  _note "RESULT: NO-ASSETS -- $_result -- zero test assets discovered in this repo."
  _note "        Nothing was verified. This zero is printed on purpose (DGN-1211): a repo"
  _note "        that should carry tests but shows NO-ASSETS is itself a finding."
  exit 0
fi
if [ "$DEGRADED" -gt 0 ]; then
  _note "RESULT: DEGRADED-PASS -- $_result -- some classes did NOT run (missing runtime, see above)."
  exit 0
fi
if [ "$SUITE_DEFER" -gt 0 ] || [ "$SUITE_MANUAL" -gt 0 ] || [ "$RED_KNOWN" -gt 0 ] || [ "$RED_STALE" -gt 0 ]; then
  _note "RESULT: PASS-WITH-GAPS -- $_result"
  _note "        PASS covers only what RAN. $SUITE_DEFER slow-lane suite(s) deferred, $SUITE_MANUAL manual,"
  _note "        $RED_KNOWN known-red (quarantined), $RED_STALE stale quarantine entr(ies). Full sweep:"
  _note "        DGN_TEST_LANE=all bash git-hooks/run-tests.sh"
  exit 0
fi
_note "RESULT: PASS -- $_result"
exit 0
