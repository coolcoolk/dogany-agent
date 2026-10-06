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
# SLOW-LANE LEDGER (DGN-1508). The stamp above records ONE WORD (green/red)
# for the whole slow lane, so between slow runs a fast push printed only
# deferred-slow=N -- a red slow suite was a red NOBODY COUNTED. Measured
# instance: tests/dgn1031_update_plan_selftest.sh went 70/23 red at 385c48b3
# (2026-09-15) and no gate output carried a number for it; it surfaced only
# because a worker happened to run the suite by hand (DGN-1508 -- the FIFTH
# silently-dead-or-red suite found that day). So every slow-lane run now
# writes a per-suite ledger next to the stamp (dogany-slow-lane.ledger:
# "<relpath> <green|red> <since-date> lane=<lane> cause=<pass|fail|timeout@Ns>"
# -- the tail since q1004; 3-field legacy rows still parse), and every FAST
# run reads it back:
#   deferred-red=N         red per the last slow run, NOT in tests/KNOWN-RED
#                          -- the unknown-red count, printed per-suite with
#                          its since-date (non-blocking by the deferred-red
#                          doctrine above: one blocked push per window, but
#                          now a NUMBER on every push in between, not silence)
#   deferred-red-known=N   red per the last slow run AND live-quarantined
#   deferred-unmeasured=N  no ledger entry -- verdict unknown (first run
#                          after this mechanism lands, or a newly laned file)
#   deferred-void=N        ledger red that is NOT a verdict on the suite as it
#                          runs now (q1004): measured on another lane (moved
#                          fast -> slow since), or a TIMEOUT under a smaller
#                          budget than it now gets. Printed per-suite as
#                          DEFERRED-VOID with the reason; never DEFERRED-RED
#                          ("per the last slow-lane run" would be false). The
#                          next slow run measures it and replaces the row.
# Newly-red is distinguishable from already-red on both sides: the slow run
# that observes a green->red transition prints NEWLY RED (no prior entry =
# FIRST-MEASURED RED: onset unknown, observation starts today), and the
# since-date persists across runs while the verdict is unchanged, so a fast
# run shows how long each red has been standing. The date is an OBSERVATION
# bound, not a true onset -- the ledger can only date what a slow run saw.
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
# IMPACT SELECTION (DGN-1734). Every push ran every fast suite whatever it
# changed: the fast lane grew 90 -> 214 suites since 09-04 and measured 651s
# per push (2026-09-26, worklog/DGN-1734-measure-report.md). pre-push now runs
# DGN_TEST_LANE=affected with DGN_TEST_CHANGED_RANGES="<local>:<remote> ...":
#   selected  a changed suite file itself; every suite whose static coverage
#             map (the suite's own path/basename references + the source/bash/
#             python3/exec/import closure of those, depth 8 -- the measured
#             rule, cached per tree under the git common dir) names a changed
#             file; every '# test-covers: <glob>...' hit (first 60 lines, may
#             only WIDEN a suite's reach; '*' = run on every push -- the cheap
#             <2s tree scanners declare that); suites named on a changed
#             tests/KNOWN-RED line; a pytest root whose parent dir holds a
#             changed file. FAST suites only: an affected SLOW suite is NOT
#             run on a push (measured 501s / 1503s pushes) -- it stays on the
#             tag (lane=all) and the staleness clocks, and the RESULT line
#             lists it as deferred with its last ledger verdict
#             (affected-slow-deferred=N + a DEFERRED-AFFECTED line each).
#   fallback  anything the map cannot vouch for runs the FULL fast lane and
#             prints why: runner/hook change (both copies), a shared file
#             (reached by >=25% of fast suites), an unmapped .sh/.py/
#             executable, a deletion/rename, a zero/unresolvable remote sha,
#             no merge-base, no range at all, python3 missing, map failure.
#   clocks    a full fast run stamps dogany-full-fast.stamp; an affected run
#             whose stamp is missing or >= DGN_FULL_FAST_MAX_AGE_H (default
#             24h) is a full fast run. A push carrying a refs/tags/v* ref runs
#             lane=all (pre-push forces it -- the dev-tag cut is the release
#             gate, by structure, not by a release.sh promise); since
#             DGN-1765 a DEV tag runs lane=devtag instead (RELEASE LANE SPEED).
#   honesty   unselected fast suites print their last ledger verdict (the
#             DGN-1508 read-back, now on the fast lane too) and count as
#             skipped-impact, which makes the RESULT PASS-WITH-GAPS, never PASS.
# DECLARED TRADE-OFF: "every push verifies everything" became "every push
# verifies what it touched; everything is verified daily and at every tag".
# A red the static map misses (paths built from variables, live-instance
# reads) surfaces at the next full run, at most 24h / one tag later.
#
# STAMP WRITES ARE PRIMARY-CHECKOUT ONLY (DGN-1734). git worktrees share the
# canonical .git, so a test run inside a worktree used to write the slow /
# full-fast stamps, the ledger and the impact-map cache into the canonical
# clone -- a worktree run of unmerged code then re-armed (or reddened) the
# clocks the real checkout reads. Reads are unchanged (a worktree still sees
# the shared clocks/ledger); writes happen only when --git-dir is the common
# dir. A worktree run says so once, on stderr.
#
# RELEASE LANE SPEED (DGN-1765, dec-196). The tag push ran every suite one
# after another: dev.59 cut measured 1h13m + 1h21m on an 8-core host.
#   parallel  lane=all|slow|devtag run suites + pytest roots on a job pool,
#             DGN_TEST_JOBS wide (default: CPU count; other lanes default 1).
#             Jobs are scheduled longest-first from the recorded durations.
#             Verdicts are processed afterwards in discovery order, so the
#             output reads the same at any width. Every job gets stdin
#             /dev/null (the serial loop used to hand suites the marker list),
#             a PRIVATE TMPDIR (two suites count files in a shared TMPDIR),
#             and default SIGINT/SIGQUIT (see _timed) -- at every width.
#   serial    a suite that shares state with another (fixed path, global
#             lock, live process table) declares, in its first 60 lines,
#                 # test-serial: <group> <why>
#             Suites of one group run back to back in ONE pool slot, never
#             concurrently with each other; they still run alongside the rest.
#   budgets   sweep lanes (all|slow|devtag, any width) use 90s fast / 1200s
#             slow / 600s pytest; push lanes keep 30 / 600 / 300. Measured
#             reason at the pool-width block below.
#   durations every executed suite's wall seconds go to dogany-test-durations
#             next to the ledger ("<relpath> <seconds> <date>"), and every
#             multi-job run prints its wall time and slowest suites.
#   devtag    pre-push sends a STABLE tag (vX.Y.Z) to lane=all and a DEV tag
#             (vX.Y.Z-dev.N) to lane=devtag. Every lane=all run from the
#             primary checkout on a clean tree writes dogany-full-run.record
#             (commit, time, failures, per-suite verdict). lane=devtag REUSES
#             those verdicts and runs only (a) what the impact selector says
#             changed since that commit (fast AND slow suites, pytest roots,
#             '*' scanners) and (b) any suite the record has no verdict for,
#             or whose quarantine has lapsed since. It REFUSES reuse and runs
#             the full parallel lane=all when the record is missing, red
#             (failures>0; quarantined known-red is not red), older than
#             DGN_DEV_TAG_REUSE_MAX_AGE_H (default 36h), from a dirty tree,
#             not an ancestor of HEAD, the tag is not HEAD, the tree is dirty,
#             or the selector itself falls back. Each reused verdict is printed
#             on a REUSED line, each re-run on a DEVTAG-RUN line, and the
#             RESULT is PASS-WITH-GAPS (reused=N) -- never PASS. A devtag run
#             never writes the record or the slow/full-fast clocks, so reuse
#             cannot chain past the 36h window.
#
# RELEASE LANE LOAD (DGN-1878, owner 2026-10-06 for v2.6.0). Measured dev
# cuts: jobs=8 doubled per-suite time, load flakes reddened the record, and a
# red record forced the next cut full -- a loop that fed itself. Three parts,
# each at its own block below:
#   width     the default sweep width is read from the host (ncpu/2 ceiling,
#             load + memory headroom, DGN-1835 dispatch-cap reading), not the
#             CPU count ("WIDTH ADAPTS TO THE HOST").
#   flake     lane=flakecheck re-runs a red record's red suites ALONE on its
#             commit and appends commit-bound, timestamped "flake" rows to the
#             record; a dev tag reuses a red record only when every red row
#             passed that way, and then re-runs the failed + affected suites.
#             No row = no flake. The stable tag still runs lane=all.
#   lock      every sweep (all|slow|devtag|flakecheck) holds one machine-wide
#             slot (the cron-guard queue, class heavy-test); a second sweep
#             queues behind it instead of overlapping it.
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
DEFER_RED=0        # deferred slow suite RED per the ledger, unquarantined (DGN-1508)
DEFER_RED_KNOWN=0  # deferred slow suite RED per the ledger, live in tests/KNOWN-RED
DEFER_UNMEASURED=0 # deferred slow suite with no ledger entry -- verdict unknown
DEFER_VOID=0       # deferred slow suite whose ledger red is void under its current lane/budget (q1004)
PY_FOUND=0
PY_RAN=0
FAST_TOTAL=0          # DGN-1734: fast-lane suites discovered
SEL_FAST=0            # fast suites this run executed
SEL_SLOW=0            # slow suites touched by an affected push and DEFERRED (not run)
SUITE_SKIPPED_IMPACT=0  # fast suites / pytest roots not selected (lane=affected)
SKIP_RED=0            # skipped-impact suite RED per the ledger, unquarantined
SKIP_RED_KNOWN=0      # ... RED and live in tests/KNOWN-RED
SKIP_UNMEASURED=0     # ... no ledger verdict

_note() { echo "[run-tests] $*" >&2; }

# ---- timeout wrapper --------------------------------------------------------
# macOS ships no timeout(1)/gtimeout(1) (measured on the estate host), so the
# budget is enforced via a python3 subprocess wrapper. rc 124 = timeout,
# rc 127 = target binary missing. python3 itself missing -> the caller treats
# the whole class as RUNNER DEGRADED before reaching here.
# DGN-1765: when _RT_DUR names a file, the wall seconds of the target are
# written there (popped from the env first, so the suite never inherits it).
# The target gets SIGINT/SIGQUIT back at their defaults: a pool job is a bash
# background job, which starts with both IGNORED, and an ignored-at-entry
# signal cannot even be trapped -- a suite that tests its own INT cleanup
# (dgn593 case c) would otherwise see a different world at any width > 1.
_timed() {
  python3 -c '
import os, signal, subprocess, sys, time
t = int(sys.argv[1])
dur = os.environ.pop("_RT_DUR", "")
def _sigdfl():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)
t0 = time.monotonic()
try:
    proc = subprocess.Popen(sys.argv[2:], start_new_session=True, preexec_fn=_sigdfl)
    rc = proc.wait(timeout=t)
except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    rc = 124
except FileNotFoundError:
    rc = 127
if dur:
    try:
        with open(dur, "w") as fh:
            fh.write("%.1f\n" % (time.monotonic() - t0))
    except OSError:
        pass
sys.exit(rc)
' "$@"
}

HAVE_PY=1
command -v python3 >/dev/null 2>&1 || HAVE_PY=0

# ---- hermetic shared-home injection (DGN-1474) ------------------------------
# scripts/pack/{pack_install,update_apply,update_seed,update_plan}.sh all
# resolve the estate shared root as
#     SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
# so a suite that drives a real (non dry-run) kit install WITHOUT setting the
# override writes crew roots into the operator's REAL ~/.dogany. Three suites
# did exactly that and left ~/.dogany/crews/{depkit-b,fookit,lifekit} debris
# that production code then picked up as a normal install target (DGN-1473).
# Per-suite hand guards cannot stop the NEXT suite from forgetting (this was
# already the third recurrence), so the harness is the enforcement point:
# every suite and every pytest root gets a FRESH throwaway shared home. A
# suite that sets its own DOGANY_SHARED_HOME (the hermetic-fixture idiom)
# still wins -- its assignment overrides this inherited default. Deliberately
# NOT covered: $HOME itself (suites need real git config etc., and a
# 2026-09-15 census found zero direct $HOME write targets in scripts/tests --
# reads of live estate data, e.g. the requires-kit G16 domain-agent conf copy, stay
# possible on purpose).
_shared_home_for() {  # <relpath> -> prints a fresh per-suite sandbox dir
  _shd="$_TMPD/shared-home/$(printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_')"
  mkdir -p "$_shd" 2>/dev/null || _shd="${TMPDIR:-/tmp}"
  printf '%s' "$_shd"
}

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
# Per-job TMPDIRs live under $_TMPD. A suite that leaves an append-only file
# (terminal-state-ledger.jsonl is chflags uappend on macOS) or a read-only dir
# would make a bare rm -rf leave them behind in the host $TMPDIR, so clear the
# flags and write bits first.
trap '_heavy_release 2>/dev/null; chflags -R nouappend,nouchg "$_TMPD" 2>/dev/null; chmod -R u+w "$_TMPD" 2>/dev/null; rm -rf "$_TMPD"' EXIT

# ---- S3 launchd domain guard (DGN-1572) -------------------------------------
# A test that mints/updates a fixture instance must never register a REAL
# unit into the owner's live launchd domain -- S1/S2 redirect the write path
# (DOGANY_LAUNCH_AGENTS_DIR), this is the backstop that catches ANY path that
# bypasses them, present or future. READ-ONLY: `launchctl list` only, never
# bootout/unload/remove -- detection and remediation stay in separate hands.
# Baseline recorded now, compared after the whole sweep below; labels already
# live before this run are the baseline and never fail this check -- only a
# label that is NEW during this run does.
_LAUNCHD_BEFORE="$_TMPD/launchd-before"; _LAUNCHD_AFTER="$_TMPD/launchd-after"
_launchd_snapshot() {  # <outfile> -- sorted com.telegram-skill-bot.* labels
  : > "$1"
  command -v launchctl >/dev/null 2>&1 || return 0
  launchctl list 2>/dev/null | awk '{print $NF}' \
    | grep -F 'com.telegram-skill-bot.' | LC_ALL=C sort -u > "$1" 2>/dev/null || true
}
_launchd_snapshot "$_LAUNCHD_BEFORE"

# DGN-1718: S3 DETECTS a leak after the fact; this PREVENTS the one shape that
# leaked (a suite reaching bridge/watchdog_setup.sh, whose launchctl bootstrap
# targets the live gui domain whatever HOME says). Every suite in this sweep
# inherits a refusing stub on the script's single launchctl seam unless it
# installs its own (tests/lib/launchd_fixture.sh fx_watchdog_launchctl_stub).
if [ -z "${WATCHDOG_SETUP_LAUNCHCTL:-}" ]; then
  cat > "$_TMPD/wd-launchctl" <<'SH'
#!/bin/sh
echo "run-tests launchctl stub (DGN-1718): refusing launchctl $*" >&2
exit 1
SH
  chmod +x "$_TMPD/wd-launchctl"
  export WATCHDOG_SETUP_LAUNCHCTL="$_TMPD/wd-launchctl"
fi

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
# files/landing/ holds PAYLOAD copies destined for other instances, not suites
# this repo runs: their imports resolve against the TARGET instance's tree, so
# collecting them here fails on a missing sibling module (DGN-1505 payload,
# 2026-09-16). Same nature as fixtures/ -- present, but not ours to execute.
fi | grep -vE '(^|/)fixtures/|(^|/)__pycache__/|^files/landing/' | LC_ALL=C sort > "$_CAND"

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
  fast|slow|all|affected|devtag|flakecheck) ;;
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
# DGN-1508: per-suite verdict ledger, next to the stamp (same sharing rule:
# all worktrees of one clone share it). "" = no git dir = no ledger, and the
# deferred slow suites are counted as unmeasured rather than assumed green.
# DGN-1625: extended to record every executed lane, not just slow, so a
# fast-lane red gets the same NEWLY RED / FIRST-MEASURED RED verdict from
# recorded history instead of an unfalsifiable hand comparison.
LEDGER="${STAMP:+${STAMP%.stamp}.ledger}"
# DGN-1734: stamps/ledger/map cache are written only from the primary
# checkout (git-dir == common dir); worktrees share the canonical .git.
STAMP_WRITE=1
if [ -n "$STAMP" ]; then
  _gd_abs="$(git -C "$ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)"
  _gc_abs="$(cd "${STAMP%/*}" 2>/dev/null && pwd -P || true)"
  [ -n "$_gd_abs" ] && _gd_abs="$(cd "$_gd_abs" 2>/dev/null && pwd -P || true)"
  if [ -z "$_gd_abs" ] || [ -z "$_gc_abs" ] || [ "$_gd_abs" != "$_gc_abs" ]; then
    STAMP_WRITE=0
    _note "NOTE: linked worktree -- stamps, ledger and impact-map cache are READ-ONLY here (they live in the shared canonical .git); run from the primary checkout to advance the clocks (DGN-1734)."
  fi
fi
_stamp_put() {  # <file> <word> -- no-op (rc 1) outside the primary checkout
  [ "$STAMP_WRITE" -eq 1 ] || return 1
  echo "$2" > "$1" 2>/dev/null
}
_ledger_entry() {  # <relpath> -> "<green|red> <since-date>", or empty
  [ -n "$LEDGER" ] && [ -f "$LEDGER" ] || return 0
  awk -v f="$1" '$1 == f { print $2, $3; exit }' "$LEDGER"
}
# Row tail (q1004): "lane=<fast|slow> cause=<pass|fail|timeout@<budget>s>" --
# WHERE and HOW the verdict was measured. Legacy 3-field rows print nothing.
_ledger_meta() {  # <relpath> -> "<lane> <cause>", or empty
  [ -n "$LEDGER" ] && [ -f "$LEDGER" ] || return 0
  awk -v f="$1" '$1 == f { l = ""; c = ""
      for (i = 4; i <= NF; i++) {
        if ($i ~ /^lane=/)  l = substr($i, 6)
        if ($i ~ /^cause=/) c = substr($i, 7)
      }
      if (l != "" && c != "") print l, c; exit }' "$LEDGER"
}
# A recorded red that is NOT a verdict on the suite as it runs now (q1004):
#   relaned -- measured on another lane (e.g. a fast-lane red, suite since
#              moved to slow): "per the last slow-lane run" would be false;
#   timeout -- a TIMEOUT under a smaller budget than the suite now gets: the
#              suite was killed, not judged, under a budget it no longer has.
# Echoes the reason (rc 0) or nothing (rc 1). Legacy rows: rc 1 (unknowable).
_ledger_red_void() {  # <relpath> <current-lane> <current-budget>
  local _m _ml _mc _mb
  _m="$(_ledger_meta "$1")"
  [ -n "$_m" ] || return 1
  _ml="${_m%% *}"; _mc="${_m#* }"
  if [ "$_ml" != "$2" ]; then
    echo "measured on the $_ml lane ($_mc), suite now runs $2"; return 0
  fi
  case "$_mc" in
    timeout@*)
      _mb="${_mc#timeout@}"; _mb="${_mb%s}"
      case "$_mb" in ''|*[!0-9]*) return 1 ;; esac
      if [ "$_mb" -lt "$3" ]; then
        echo "TIMEOUT under a ${_mb}s budget, suite now gets ${3}s"; return 0
      fi
      ;;
  esac
  return 1
}
_ledger_merge_into() {  # DGN-1625: writes the merged ledger to stdout --
  # old rows this run did NOT visit (and whose suite file still exists) in
  # their original order, followed by every row this run just recorded. A
  # fast run only visits fast suites, so this can no longer be a whole-file
  # replace without silently destroying every other lane's history.
  if [ -f "$LEDGER" ]; then
    while IFS= read -r _lline; do
      [ -n "$_lline" ] || continue
      _lrel="${_lline%% *}"
      awk -v f="$_lrel" '$1==f{found=1} END{exit !found}' "$_TMPD/ledger.new" && continue
      [ -e "$ROOT/$_lrel" ] || continue
      printf '%s\n' "$_lline"
    done < "$LEDGER"
  fi
  cat "$_TMPD/ledger.new"
}

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

# ---- impact selection (DGN-1734) --------------------------------------------
# The selector is embedded (not a sibling file) so the byte-identical
# single-variant pair above stays the whole delivery unit: update.sh 3k1 and
# mint.sh already ship exactly this file.
_impact_select() {  # <markers> <pyroots> <cache> <key> -> SEL/PY/FALLBACK/INFO lines
  python3 - "$ROOT" "$@" <<'PY'
# DGN-1734 impact selector (embedded in git-hooks/run-tests.sh; read by it only).
# argv: ROOT MARKERS PYROOTS CACHE_FILE CACHE_KEY [RANGES]
# env:  DGN_TEST_CHANGED_RANGES = "<local-sha>:<remote-sha> ..." (pre-push);
#       a RANGES argv (DGN-1765 dev-tag lane) replaces it
# out:  "SEL <relpath>" | "PY <pytest-root>" | "FALLBACK <reason>" | "INFO <k>=<v>"
import collections, fnmatch, json, os, re, subprocess, sys

ROOT, MARKERS, PYROOTS, CACHE, KEY = sys.argv[1:6]
ZERO = '0' * 40
LANE_RE = re.compile(r'^#[ \t]*test-lane:[ \t]*(\S*)')
COVERS_RE = re.compile(r'^#[ \t]*test-covers:[ \t]*(.*)$')
RUNNER_FILES = ('git-hooks/run-tests.sh', 'git-hooks/pre-push',
                'agents/.template/git-hooks/run-tests.sh',
                'agents/.template/git-hooks/pre-push')
SHARED_FRAC = 0.25


def git(*a):
    return subprocess.run(('git', '-C', ROOT) + a, capture_output=True, text=True)


def head60(rel):
    try:
        with open(os.path.join(ROOT, rel), errors='replace') as fh:
            return [next(fh, '') for _ in range(60)]
    except OSError:
        return []


def build_map():
    # Same static rule as the DGN-1734 measurement (worklog measure report):
    # a suite covers (a) every tracked file its body names by path/basename
    # and (b) the execution closure (source/bash/python3/exec/import) of
    # those, depth 8. Transitive TEXT references are NOT followed -- that
    # blew up to a median 514 files per suite (mint.sh names the template).
    files = [f for f in git('ls-files').stdout.split('\n')
             if f and not f.startswith('files/landing/')]
    fset = set(files)
    bn = collections.defaultdict(list)
    for f in files:
        bn[os.path.basename(f)].append(f)
    tok = re.compile(r'[A-Za-z0-9_.\-/]+')
    imp = re.compile(r'^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w.]*))', re.M)
    ex = re.compile(r'(?:^|[\s;&|(])(?:source|\.|bash|sh|python3|exec)\s+"?([^\s";)]+)'
                    r'|^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))', re.M)
    texts = {}

    def read(f):
        if f not in texts:
            try:
                with open(os.path.join(ROOT, f), errors='replace') as fh:
                    texts[f] = fh.read()
            except OSError:
                texts[f] = ''
        return texts[f]

    def refs(f):
        s = read(f)
        out = set()
        toks = set(tok.findall(s))
        for m in imp.finditer(s):
            toks.add((m.group(1) or m.group(2)).split('.')[-1] + '.py')
        for t in toks:
            t = t.strip('./-')
            if not t:
                continue
            if '/' in t:
                parts = t.split('/')
                for i in range(len(parts)):
                    suf = '/'.join(parts[i:])
                    if suf in fset:
                        out.add(suf)
                        break
                    if len(parts) - i >= 2:
                        c = [x for x in bn.get(parts[-1], []) if x.endswith('/' + suf)]
                        if len(c) == 1:
                            out.add(c[0])
                            break
            else:
                c = bn.get(t)
                if c and '.' in t and len(t) >= 6 and len(c) <= 2:
                    out.update(c)
        out.discard(f)
        return out

    xcache = {}

    def xedges(f):
        if f in xcache:
            return xcache[f]
        out = set()
        for m in ex.finditer(read(f)):
            t = m.group(1)
            if t:
                b = re.sub(r'[^A-Za-z0-9_.\-]', '', os.path.basename(t.replace('"', '').replace("'", '')))
                cands = bn.get(b, [])
            else:
                cands = bn.get((m.group(2) or m.group(3)).split('.')[-1] + '.py', [])
            out.update(c for c in cands if c != f)
        xcache[f] = out
        return out

    def closure(s):
        seen = {s}
        fr = {s}
        for _ in range(8):
            nx = set()
            for x in fr:
                for y in xedges(x):
                    if y not in seen:
                        seen.add(y)
                        nx.add(y)
            fr = nx
        seen.discard(s)
        return seen

    lanes, covers, cov = {}, {}, {}
    for rel in open(MARKERS).read().split():
        lines = head60(rel)
        lane = ''
        pats = []
        for ln in lines:
            m = LANE_RE.match(ln)
            if m and not lane:
                lane = m.group(1)
            m = COVERS_RE.match(ln)
            if m:
                for p in m.group(1).split():
                    if p.startswith('#'):
                        break
                    pats.append(p)
        lanes[rel] = lane
        if pats:
            covers[rel] = pats
        if lane in ('fast', 'slow'):
            base = refs(rel) | xedges(rel)
            full = set(base)
            for b in list(base):
                full |= closure(b)
            full |= closure(rel)
            full.discard(rel)
            cov[rel] = sorted(full)
    return {'lanes': lanes, 'covers': covers, 'cov': cov}


def load_map():
    try:
        with open(CACHE) as fh:
            d = json.load(fh)
        if d.get('key') == KEY:
            return d['map'], 'hit'
    except (OSError, ValueError, KeyError):
        pass
    m = build_map()
    if KEY:
        try:
            with open(CACHE + '.tmp', 'w') as fh:
                json.dump({'key': KEY, 'map': m}, fh)
            os.replace(CACHE + '.tmp', CACHE)
        except OSError:
            pass
    return m, 'built'


def changes():
    """-> (list of (status, path), knownred_diff_text, fallback-reason|None)"""
    rngs = (sys.argv[6] if len(sys.argv) > 6
            else os.environ.get('DGN_TEST_CHANGED_RANGES', '')).split()
    if not rngs:
        return [], '', 'no-range (DGN_TEST_CHANGED_RANGES empty)'
    out, kr = {}, []
    for r in rngs:
        loc, _, rem = r.partition(':')
        if not rem or rem == ZERO:
            return [], '', 'new-ref (remote sha is zero: range unbounded)'
        for s in (loc, rem):
            if git('cat-file', '-e', s + '^{commit}').returncode != 0:
                return [], '', 'range-unresolvable (%s not in local objects)' % s[:12]
        mb = git('merge-base', rem, loc)
        if mb.returncode != 0 or not mb.stdout.strip():
            return [], '', 'no-merge-base (%s..%s)' % (rem[:12], loc[:12])
        base = mb.stdout.strip()
        d = git('diff', '--name-status', '--no-renames', base, loc)
        if d.returncode != 0:
            return [], '', 'range-unresolvable (diff failed)'
        for ln in d.stdout.splitlines():
            st, _, p = ln.partition('\t')
            if p:
                out[p] = 'D' if out.get(p) == 'D' else st[:1]
        kr.append(git('diff', base, loc, '--', 'tests/KNOWN-RED').stdout)
    return sorted((s, p) for p, s in out.items()), '\n'.join(kr), None


def main():
    ch, krdiff, why = changes()
    if why:
        print('FALLBACK ' + why)
        return
    m, cstate = load_map()
    lanes, covers, cov = m['lanes'], m['covers'], m['cov']
    fast = [s for s, l in lanes.items() if l == 'fast']
    hits = collections.defaultdict(set)
    for s, fs in cov.items():
        for f in fs:
            hits[f].add(s)
    fan = collections.Counter(f for s in fast for f in cov.get(s, ()))
    thr = max(2, int(len(fast) * SHARED_FRAC))
    shared = {f for f, n in fan.items() if n >= thr}
    pyroots = [p for p in open(PYROOTS).read().split() if p]
    modes = {}
    if ch:
        ls = git('ls-files', '-s', '--', *[p for _, p in ch])
        for ln in ls.stdout.splitlines():
            meta, _, p = ln.partition('\t')
            modes[p] = meta.split()[0]
    print('INFO changed=%d map=%s shared=%d' % (len(ch), cstate, len(shared)))
    sel, py, fb = set(), set(), []
    for s, pats in covers.items():
        if '*' in pats:
            sel.add(s)
    for st, p in ch:
        if st == 'D':
            fb.append('deleted:' + p)
            continue
        if p in RUNNER_FILES:
            fb.append('runner:' + p)
            continue
        for s, pats in covers.items():
            if any(fnmatch.fnmatchcase(p, g) for g in pats):
                sel.add(s)
        in_py = False
        for r in pyroots:
            parent = os.path.dirname(r)
            if p.startswith((parent or r) + '/'):
                py.add(r)
                in_py = True
        if p == 'tests/KNOWN-RED':
            for ln in krdiff.splitlines():
                if ln[:1] in '+-' and not ln.startswith(('+++', '---')):
                    t = ln[1:].split()
                    if t and lanes.get(t[0]) in ('fast', 'slow'):
                        sel.add(t[0])
            continue
        lane = lanes.get(p)
        if lane in ('fast', 'slow'):
            sel.add(p)
            continue
        if lane == 'manual':
            continue
        if p in shared:
            fb.append('shared:' + p)
            continue
        if hits.get(p):
            sel |= hits[p]
            continue
        if lane == 'lib':
            fb.append('unmapped-lib:' + p)
            continue
        if in_py or '/fixtures/' in '/' + p or p.startswith('files/landing/'):
            continue
        code = p.endswith(('.sh', '.py')) or modes.get(p) == '100755'
        if code:
            fb.append('unmapped:' + p)
    if fb:
        for r in fb:
            print('FALLBACK ' + r)
        return
    for s in sorted(sel):
        print('SEL ' + s)
    for r in sorted(py):
        print('PY ' + r)


main()
PY
}
FULL_FAST_MAX_AGE_H="${DGN_FULL_FAST_MAX_AGE_H:-24}"
FULLFAST_STAMP="${STAMP:+${STAMP%/*}/dogany-full-fast.stamp}"
FULLFAST_AGE="n/a"
if [ -n "$FULLFAST_STAMP" ]; then
  if [ -f "$FULLFAST_STAMP" ]; then
    FULLFAST_AGE="$(_age_hours "$FULLFAST_STAMP")"
  else
    FULLFAST_AGE="never"
  fi
fi
IMPACT_FALLBACK="none"
IMPACT_SEL="$_TMPD/impact.sel"; : > "$IMPACT_SEL"
IMPACT_PY="$_TMPD/impact.pyroots"; : > "$IMPACT_PY"
# Runs the embedded selector and sets IMPACT_FALLBACK ("none" = IMPACT_SEL /
# IMPACT_PY hold the selection). [ranges] overrides DGN_TEST_CHANGED_RANGES
# (DGN-1765: the dev-tag lane diffs against the last full run, not the push).
_impact_run() {
  # Cache key: tree + dirt + this runner's own bytes (a rule change must
  # never read a map built by the previous rule).
  _ikey="$(git -C "$ROOT" rev-parse "HEAD^{tree}" 2>/dev/null)"
  if [ "$STAMP_WRITE" -eq 1 ] && [ -n "$_ikey" ] && [ -z "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]; then
    _ikey="$_ikey.$(git hash-object "$SELF_DIR/run-tests.sh" 2>/dev/null)"
  else
    _ikey=""
  fi
  _iout="$_TMPD/impact.out"
  if _impact_select "$_MARKERS" "$_PYROOTS" "${STAMP%/*}/dogany-impact-map.json" "$_ikey" "$@" > "$_iout" 2>"$_TMPD/impact.err"; then
    if grep -q '^FALLBACK ' "$_iout"; then
      IMPACT_FALLBACK="$(grep -m1 '^FALLBACK ' "$_iout" | cut -d' ' -f2- | tr ' ' '_')"
      _nfb="$(grep -c '^FALLBACK ' "$_iout")"
      [ "$_nfb" -gt 1 ] && IMPACT_FALLBACK="$IMPACT_FALLBACK(+$((_nfb - 1)))"
      grep '^FALLBACK ' "$_iout" | head -10 | while IFS= read -r _l; do _note "impact: ${_l}"; done
    else
      sed -n 's/^SEL //p' "$_iout" > "$IMPACT_SEL"
      sed -n 's/^PY //p' "$_iout" > "$IMPACT_PY"
      _note "impact: $(sed -n 's/^INFO //p' "$_iout" | head -1) selected=$(wc -l < "$IMPACT_SEL" | tr -d ' ') suite(s) + $(wc -l < "$IMPACT_PY" | tr -d ' ') pytest root(s)"
    fi
  else
    IMPACT_FALLBACK="map-failed"
    tail -3 "$_TMPD/impact.err" | while IFS= read -r _l; do _note "impact: $_l"; done
  fi
}
if [ "$LANE" = "affected" ]; then
  if [ -z "$FULLFAST_STAMP" ]; then
    IMPACT_FALLBACK="no-git-dir"
  elif [ "$FULLFAST_AGE" = "never" ] || [ -z "$FULLFAST_AGE" ]; then
    IMPACT_FALLBACK="full-fast-clock:${FULLFAST_AGE:-unreadable}"
  elif [ "$FULLFAST_AGE" -ge "$FULL_FAST_MAX_AGE_H" ]; then
    IMPACT_FALLBACK="full-fast-clock:${FULLFAST_AGE}h>=${FULL_FAST_MAX_AGE_H}h"
  elif [ "$HAVE_PY" -ne 1 ]; then
    IMPACT_FALLBACK="no-python3"
  else
    _impact_run
  fi
  if [ "$IMPACT_FALLBACK" != "none" ]; then
    _note "IMPACT FALLBACK ($IMPACT_FALLBACK) -- running the FULL fast lane (DGN-1734)."
    LANE=fast
  fi
fi
case "$FULLFAST_AGE" in ''|*[!0-9]*) FULLFAST_AGE="${FULLFAST_AGE:-unreadable}" ;; *) FULLFAST_AGE="${FULLFAST_AGE}h" ;; esac
_impact_selected() { grep -qxF "$1" "$IMPACT_SEL"; }

# ---- full-run record + dev-tag verdict reuse (DGN-1765) ---------------------
# The record is written by every lane=all run from the primary checkout (see
# the verdict section); it is the ONLY thing lane=devtag may reuse.
FULLRUN_REC="${STAMP:+${STAMP%/*}/dogany-full-run.record}"
DUR_FILE="${STAMP:+${STAMP%/*}/dogany-test-durations}"
DEV_REUSE_MAX_AGE_H="${DGN_DEV_TAG_REUSE_MAX_AGE_H:-36}"
REUSE_BASE="none"
REUSED=0             # suite / pytest-root verdicts taken from the record, not run
REUSED_RED_KNOWN=0   # ... of which red-known (still live in tests/KNOWN-RED)
DT_RAN=0             # suites / pytest roots a devtag run executed
_START_HEAD="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
_START_CLEAN=0
[ -n "$_START_HEAD" ] && [ -z "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ] && _START_CLEAN=1
_rec_field() {  # <key> -> value from the record header
  awk -v k="$1" '$1 == k { print $2; exit }' "$FULLRUN_REC" 2>/dev/null
}
_rec_row() {  # <suite|pytest> <relpath> -> recorded verdict, or empty
  awk -v k="$1" -v f="$2" '$1 == k && $2 == f { print $3; exit }' "$FULLRUN_REC" 2>/dev/null
}
# DGN-1878 item 2: a RED record is reusable by a dev cut only when EVERY red
# row carries a recorded flake verdict -- "flake <kind> <relpath> pass <commit>
# <epoch> <iso-date> rc=<n> dur=<s>", appended to THIS record by
# lane=flakecheck (below), which re-ran that suite ALONE on the record's own
# commit after the record was written. Assumed flakes do not count: no row,
# a row bound to another commit / an older record, or a failed re-run keeps
# the record red. A failure that is not a suite row (UNLANED, parity break,
# launchd S3) cannot be re-run alone, so failures must equal the red rows.
# rc 0 + the red-row count when cleared; rc 1 + the reason otherwise.
_flake_cleared() {
  awk -v c="$_rc_commit" -v e="$_rc_epoch" -v f="$_rc_fail" '
    ($1 == "suite" || $1 == "pytest") && $3 == "red" { red[$1 " " $2] = 1; n++ }
    $1 == "flake" { k = $2 " " $3; fv[k] = $4; fc[k] = $5; fe[k] = $6 }
    END {
      if (n == 0 || n != f + 0) {
        printf "failures=%s but %d red suite row(s): a non-suite failure cannot be re-run alone\n", f, n; exit 1 }
      for (k in red) {
        split(k, a, " ")
        if (!(k in fv))     { print "no recorded flake verdict for " a[2]; exit 1 }
        if (fc[k] != c || fe[k] + 0 < e + 0) {
                              print "flake verdict for " a[2] " is not bound to this record (" substr(fc[k], 1, 12) ")"; exit 1 }
        if (fv[k] != "pass") { print a[2] " re-run ALONE and failed again: a real red, not a flake"; exit 1 }
      }
      print n
    }' "$FULLRUN_REC" 2>/dev/null
}
FLAKE_CLEARED=0
if [ "$LANE" = "devtag" ]; then
  _dt_why=""
  if [ -z "$FULLRUN_REC" ] || [ ! -f "$FULLRUN_REC" ]; then
    _dt_why="no-full-run-record"
  elif [ "$HAVE_PY" -ne 1 ]; then
    _dt_why="no-python3"
  else
    _rc_commit="$(_rec_field commit)"; _rc_epoch="$(_rec_field epoch)"
    _rc_fail="$(_rec_field failures)"; _rc_clean="$(_rec_field clean)"
    _now="$(date +%s)"
    case "$_rc_epoch" in
      ''|*[!0-9]*) _rc_age="" ;;
      *) if [ "$_rc_epoch" -gt "$_now" ]; then _rc_age=""; else _rc_age=$(( (_now - _rc_epoch) / 3600 )); fi ;;
    esac
    if [ -z "$_rc_commit" ] || [ -z "$_rc_age" ] || [ -z "$_rc_fail" ]; then
      _dt_why="record-unreadable"
    elif [ "$_rc_fail" != "0" ] && ! _flk="$(_flake_cleared)"; then
      _dt_why="record-red(failures=$_rc_fail)"
      _note "FLAKE: red record NOT cleared -- ${_flk:-flake verdicts unreadable}. A flake is RECORDED, never assumed:"
      _note "       on the record's commit, primary checkout: DGN_TEST_LANE=flakecheck bash git-hooks/run-tests.sh (DGN-1878)"
    elif [ "$_rc_age" -ge "$DEV_REUSE_MAX_AGE_H" ]; then
      _dt_why="record-stale(${_rc_age}h>=${DEV_REUSE_MAX_AGE_H}h)"
    elif [ "$_rc_clean" != "1" ]; then
      _dt_why="record-from-dirty-tree"
    elif [ "$_START_CLEAN" -ne 1 ]; then
      _dt_why="dirty-tree"
    elif [ -n "${DGN_TEST_TAG_SHA:-}" ] && [ "$DGN_TEST_TAG_SHA" != "$_START_HEAD" ]; then
      _dt_why="tag-not-HEAD(${DGN_TEST_TAG_SHA:0:12})"
    elif ! git -C "$ROOT" merge-base --is-ancestor "$_rc_commit" "$_START_HEAD" 2>/dev/null; then
      _dt_why="record-not-ancestor(${_rc_commit:0:12})"
    else
      _impact_run "$_START_HEAD:$_rc_commit"
      [ "$IMPACT_FALLBACK" != "none" ] && _dt_why="selector:$IMPACT_FALLBACK"
    fi
  fi
  if [ -n "$_dt_why" ]; then
    _note "DEV-TAG REUSE REFUSED ($_dt_why) -- running the FULL lane (lane=all) (DGN-1765)."
    IMPACT_FALLBACK="devtag-refused:$_dt_why"
    : > "$IMPACT_SEL"; : > "$IMPACT_PY"
    LANE=all
  else
    REUSE_BASE="${_rc_commit:0:12}@${_rc_age}h"
    if [ "$_rc_fail" != "0" ]; then
      FLAKE_CLEARED="$_flk"; REUSE_BASE="$REUSE_BASE+flake=$_flk"
      _note "DEV-TAG REUSE over a RED record: failures=$_rc_fail, every red suite re-ran ALONE on ${_rc_commit:0:12} and passed (recorded flake verdicts):"
      awk '$1 == "flake" { print $3 " passed alone at " $7 " commit " substr($5, 1, 12) " " $9 }' "$FULLRUN_REC" 2>/dev/null \
        | while IFS= read -r _l; do _note "       FLAKE-VERDICT: $_l"; done
      _note "       those suites RE-RUN on this cut (record-verdict-red), with the affected set; nothing else is widened (DGN-1878)."
    fi
    _note "DEV-TAG REUSE: full run ${_rc_commit:0:12} (${_rc_age}h old, failures=$_rc_fail, < ${DEV_REUSE_MAX_AGE_H}h) -- its verdicts are reused; changed-since and unverdicted suites are re-run (DGN-1765)."
  fi
fi

# ---- flake verdicts for a red full-run record (DGN-1878 item 2) -------------
# lane=flakecheck re-runs EVERY red suite / pytest root of the full-run record
# ALONE (one slot, under the machine-wide heavy lock, sweep budgets -- the
# budgets the record's verdicts were judged by) on the record's OWN commit,
# from a clean primary checkout, and appends one "flake ..." row per re-run to
# that record (format at _flake_cleared). Only that record row is evidence; a
# dev cut reads it back (_flake_cleared) and runs failed + affected instead of
# the full lane. It never touches the ledger, the clocks or the durations: it
# re-judges nothing, it adds a timestamped, commit-bound fact. The stable cut
# is untouched -- a vX.Y.Z tag still runs lane=all whatever this records.
FLAKE_PASS=0; FLAKE_FAIL=0
if [ "$LANE" = "flakecheck" ]; then
  _fc_why=""
  _rc_commit="$(_rec_field commit)"; _rc_fail="$(_rec_field failures)"; _rc_clean="$(_rec_field clean)"
  if [ -z "$FULLRUN_REC" ] || [ ! -f "$FULLRUN_REC" ]; then
    _fc_why="no-full-run-record"
  elif [ "$STAMP_WRITE" -ne 1 ]; then
    _fc_why="linked-worktree (the record is read-only here)"
  elif [ "$HAVE_PY" -ne 1 ]; then
    _fc_why="no-python3"
  elif [ -z "$_rc_commit" ] || [ -z "$_rc_fail" ]; then
    _fc_why="record-unreadable"
  elif [ "$_rc_clean" != "1" ]; then
    _fc_why="record-from-dirty-tree"
  elif [ "$_START_CLEAN" -ne 1 ]; then
    _fc_why="dirty-tree"
  elif [ "$_rc_commit" != "$_START_HEAD" ]; then
    _fc_why="HEAD-is-not-the-record-commit(${_rc_commit:0:12})"
  fi
  awk '($1 == "suite" || $1 == "pytest") && $3 == "red" { print $1, $2 }' "${FULLRUN_REC:-/dev/null}" 2>/dev/null > "$_TMPD/flake.todo"
  if [ -z "$_fc_why" ] && [ ! -s "$_TMPD/flake.todo" ]; then
    _fc_why="no-red-suite-rows(failures=$_rc_fail)"
  fi
  if [ -n "$_fc_why" ]; then
    _note "FLAKE-CHECK REFUSED ($_fc_why) -- nothing re-run, nothing recorded (DGN-1878)."
    _note "RESULT: FAIL -- lane=flakecheck refused=$_fc_why flake-pass=0 flake-fail=0"
    exit 1
  fi
  _note "FLAKE-CHECK: re-running $(wc -l < "$_TMPD/flake.todo" | tr -d ' ') red row(s) of full run ${_rc_commit:0:12} (failures=$_rc_fail) ALONE, one at a time (DGN-1878)."
fi

# The slow lane's reader: a clock, not a promise. A fast-lane run whose stamp
# is missing or stale escalates ITSELF and says so. No scheduler to forget.
SLOW_ESCALATED=0
if { [ -s "$_MARKERS" ] || [ -s "$_UNLANED" ]; } && { [ "$LANE" = "fast" ] || [ "$LANE" = "affected" ]; }; then
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

[ "$SLOW_ESCALATED" -eq 1 ] && [ "$IMPACT_FALLBACK" = "none" ] && [ "${DGN_TEST_LANE:-}" = "affected" ] \
  && IMPACT_FALLBACK="slow-clock"

# Stamp the ATTEMPT before running, not the success after it. If a red slow
# lane left the clock stale, EVERY subsequent push would re-pay the whole
# 400s lane on top of already being blocked. The verdict is written into the
# stamp so a deferred push still says whether the last slow run was green.
if [ "$SLOW_ESCALATED" -eq 1 ] && [ -n "$STAMP" ]; then
  _stamp_put "$STAMP" attempted || true
fi
# DGN-1734: same attempt-first rule for the full-fast clock. Every lane but
# 'affected' runs every fast suite, so every such run resets it.
if [ "$LANE" != "affected" ] && [ "$LANE" != "slow" ] && [ "$LANE" != "devtag" ] && [ "$LANE" != "flakecheck" ] && [ -n "$FULLFAST_STAMP" ] && [ -s "$_MARKERS" ]; then
  _stamp_put "$FULLFAST_STAMP" attempted || true
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

# ---- machine-wide heavy-lane lock (DGN-1878 item 3) -------------------------
# Measured 2026-10-05/06: two crew agents cut/sweep from different clones on
# ONE Mac, and two overlapping sweeps each ran ~2x slower, timed suites out,
# reddened the record, and forced the next dev cut to the full lane again.
# So a sweep (lane=all|slow|devtag|flakecheck) holds ONE machine-wide slot for
# its whole life; a second sweep QUEUES behind it (never fails) and says who
# holds it. This is the cron-guard machine-global queue (DGN-360), not a new
# lock: same home ~/.dogany/cron-queue/<class>/slot-N/, same atomic-mkdir +
# pidfile, same dead-pid / pidfile-less-after-60s reclaim, same fail-open
# after the wait budget (DGN_HEAVY_LOCK_TIMEOUT, default 7200s -- ~3 measured
# full sweeps -- then the run proceeds LOUD, unlocked: a stuck holder must not
# turn into a stuck release). Class "heavy-test", one slot. The home follows
# DOGANY_SHARED_HOME (DGN-1474), so a runner nested inside a suite -- which
# the harness gives a throwaway shared home -- never touches the real queue;
# DGN_HEAVY_LOCK_HELD (exported to every job) makes a nested sweep under the
# holder re-entrant instead of a self-deadlock. Fast/affected pushes never
# take it: they are the cheap lane and must not wait 40 minutes on a sweep.
HEAVY_LOCK="none"
_HEAVY_SLOT=""
_heavy_release() {
  [ -n "$_HEAVY_SLOT" ] || return 0
  [ "$(cat "$_HEAVY_SLOT/pid" 2>/dev/null)" = "$$" ] && rm -rf "$_HEAVY_SLOT" 2>/dev/null
  _HEAVY_SLOT=""
}
_heavy_try() {  # <slot-dir> -> rc 0 acquired (cron-guard try_queue_slot shape)
  local _o _o2 _mt
  if mkdir "$1" 2>/dev/null; then
    echo "$$" > "$1/pid"
    printf '%s lane=%s root=%s since=%s\n' "$$" "$LANE" "$ROOT" "$(date +%s)" > "$1/who"
    _HEAVY_SLOT="$1"; return 0
  fi
  _o="$(cat "$1/pid" 2>/dev/null || true)"
  if [ -n "$_o" ]; then
    kill -0 "$_o" 2>/dev/null && return 1
    _o2="$(cat "$1/pid" 2>/dev/null || true)"
    [ "$_o2" = "$_o" ] || return 1
    _note "heavy lock: reclaimed stale slot (dead pid $_o): $1"
  else
    _mt="$(stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || date +%s)"
    _mt=$(( $(date +%s) - _mt ))
    [ "$_mt" -gt 60 ] || return 1
    _note "heavy lock: reclaimed abandoned slot (no pidfile, age ${_mt}s): $1"
  fi
  # Atomic rename first, so two reclaimers never both remove a fresh slot.
  mv "$1" "${1%/*}/.reclaim.$$.$RANDOM" 2>/dev/null || return 1
  rm -rf "${1%/*}/.reclaim.$$."* 2>/dev/null
  mkdir "$1" 2>/dev/null || return 1
  echo "$$" > "$1/pid"
  printf '%s lane=%s root=%s since=%s\n' "$$" "$LANE" "$ROOT" "$(date +%s)" > "$1/who"
  _HEAVY_SLOT="$1"
}
case "$LANE" in
  all|slow|devtag|flakecheck)
    if [ -n "${DGN_HEAVY_LOCK_HELD:-}" ]; then
      HEAVY_LOCK="inherited"
    elif [ -s "$_MARKERS" ] || [ -s "$_PYROOTS" ]; then
      _hdir="${DOGANY_SHARED_HOME:-${HOME:-}/.dogany}/cron-queue/heavy-test"
      _htmo="${DGN_HEAVY_LOCK_TIMEOUT:-7200}"
      case "$_htmo" in ''|*[!0-9]*) _htmo=7200 ;; esac
      _hpoll="${DGN_HEAVY_LOCK_POLL:-5}"
      case "$_hpoll" in ''|*[!0-9]*|0) _hpoll=5 ;; esac
      if ! mkdir -p "$_hdir" 2>/dev/null; then
        HEAVY_LOCK="unavailable"
        _note "heavy lock: queue home NOT writable ($_hdir) -- running UNLOCKED (DGN-1878)."
      else
        _hw0="$(date +%s)"; _hlast=-1
        while ! _heavy_try "$_hdir/slot-1"; do
          _hwait=$(( $(date +%s) - _hw0 ))
          if [ "$_hwait" -ge "$_htmo" ]; then
            HEAVY_LOCK="timeout"
            _note "heavy lock: WAITED ${_hwait}s (>= ${_htmo}s) behind $(cat "$_hdir/slot-1/who" 2>/dev/null || echo 'an unknown holder') -- running anyway, UNLOCKED (fail-open, cron-guard DGN-360 semantics)."
            break
          fi
          if [ $((_hwait / 300)) -ne "$_hlast" ]; then
            _hlast=$((_hwait / 300))
            _note "heavy lock: QUEUED ${_hwait}s -- another sweep holds the machine-wide test slot: $(cat "$_hdir/slot-1/who" 2>/dev/null || echo '(holder unreadable)') (DGN-1878; waits, never fails)"
          fi
          sleep "$_hpoll"
        done
        if [ -n "$_HEAVY_SLOT" ]; then
          _hwait=$(( $(date +%s) - _hw0 ))
          HEAVY_LOCK="held(waited=${_hwait}s)"
          export DGN_HEAVY_LOCK_HELD="$$"
          _note "heavy lock: holding the machine-wide test slot after ${_hwait}s ($_HEAVY_SLOT)"
        fi
      fi
    fi
    ;;
esac

# ---- job pool width + sweep budgets (DGN-1765) ------------------------------
# Decided HERE, after the slow clock may have escalated a push to lane=all.
# Sweep lanes (all|slow|devtag) get the pool AND wider budgets, at ANY width
# (JOBS=1 included), so serial and parallel sweeps judge by the same clock.
# Measured 2026-09-28 (8 jobs, host load 5-26): suites ran ~1.8x their serial
# time (9653 suite-seconds for a 5477s serial sweep) -- dgn1734 (14s alone)
# crossed the 30s fast budget and dgn1031_update_apply (480s alone) crossed
# the 600s slow budget, both green serially. A budget is a HANG detector, not
# a speed contract: the fast lane's 30s speed contract stays enforced where it
# matters, on every push (fast/affected keep 30/600/300). DECLARED TRADE-OFF:
# a fast suite that grew to 30-90s is no longer a TIMEOUT in a sweep -- the
# next push that runs it still says so.
#
# WIDTH ADAPTS TO THE HOST (DGN-1878 item 1). The default width used to be the
# CPU count. Measured 2026-10-05/06 on the 8-core estate host: jobs=8 -> pool
# wall 2415s / 2521s for 19252 / 20112 suite-seconds with load-induced
# timeouts; jobs=4 -> wall 2131s for 8463 suite-seconds, 0 failures. Eight
# slots more than doubled every suite's time and bought nothing. The default
# is now the TIGHTEST of three headrooms, read once at pool start with the
# DGN-1835 dispatch-cap machine reading (load5 + ncpu + available MB from
# kern.memorystatus_level x hw.memsize / Linux MemAvailable), never below 1:
#   ceiling  ncpu / 2                         (the measured sustainable width)
#   load     (ncpu x 1.5 - load5) / 2 per slot (1.5/ncpu = the dispatch cap's
#            machine alert line; 2 load per slot = jobs=8 drove load 5-26)
#   memory   (available MB - 1024) / 512 per slot (dispatch cap constants)
# A read failure falls back to the ceiling (announced), never to ncpu.
# An explicit DGN_TEST_JOBS is an operator choice and is honored as given; a
# value above the host's width is printed as such. DECLARED TRADE-OFF: the
# width is decided once, at pool start (after the heavy lock wait above, so
# the reading is current) -- load that arrives mid-sweep is not re-read.
_ncpu="$(sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
case "$_ncpu" in ''|*[!0-9]*|0) _ncpu=1 ;; esac
_machine_snapshot() {  # stdout "<load5> <ncpu> <available MB>"; rc 1 on failure
  # DGN_TEST_MACHINE="<load5> <ncpu> <MB>" replaces the live read (selftests).
  if [ -n "${DGN_TEST_MACHINE+x}" ]; then
    printf '%s\n' "$DGN_TEST_MACHINE" | awk 'NF == 3 && $1 ~ /^[0-9.]+$/ && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ { print; ok = 1 } END { exit !ok }'
    return
  fi
  [ "$HAVE_PY" -eq 1 ] || return 1
  python3 - <<'PY' 2>/dev/null
import os, subprocess, sys
try:
    load5 = os.getloadavg()[1]
    ncpu = os.cpu_count()
    if sys.platform == "darwin":
        sysctl = lambda k: int(subprocess.check_output(["sysctl", "-n", k], text=True))
        avail = sysctl("hw.memsize") * sysctl("kern.memorystatus_level") / 100
    else:
        with open("/proc/meminfo") as f:
            avail = next(int(l.split()[1]) * 1024 for l in f if l.startswith("MemAvailable:"))
    print("%.2f" % load5, ncpu, int(avail // 2**20))
except Exception:
    sys.exit(1)
PY
}
_pool_width() {  # -> "<jobs> <why...>"
  local _snap
  if ! _snap="$(_machine_snapshot)"; then
    _c=$(( _ncpu / 2 )); [ "$_c" -ge 1 ] || _c=1
    echo "$_c machine read FAILED -- ceiling ncpu/2=$_c used"
    return 0
  fi
  printf '%s\n' "$_snap" | awk '{
    load5 = $1; ncpu = $2; avail = $3
    ceil = int(ncpu / 2); if (ceil < 1) ceil = 1
    bl = (ncpu * 1.5 - load5) / 2; bl = (bl < 0) ? 0 : int(bl)
    bm = (avail - 1024) / 512;     bm = (bm < 0) ? 0 : int(bm)
    j = ceil; if (bl < j) j = bl; if (bm < j) j = bm; if (j < 1) j = 1
    printf "%d load5=%s/%g (+%d) mem_avail=%dMB (+%d) ceiling=ncpu/2=%d -> %d\n", j, load5, ncpu * 1.5, bl, avail, bm, ceil, j }'
}
PY_BUDGET=300
case "$LANE" in
  flakecheck)
    # "re-run ALONE" is the whole point: one slot, whatever DGN_TEST_JOBS says.
    JOBS=1
    FAST_BUDGET="${DGN_FAST_BUDGET:-90}"; SLOW_BUDGET="${DGN_SLOW_BUDGET:-1200}"; PY_BUDGET=600
    ;;
  all|slow|devtag)
    read -r _pw _pwhy <<<"$(_pool_width)"
    if [ -n "${DGN_TEST_JOBS:-}" ]; then
      JOBS="$DGN_TEST_JOBS"
      case "$JOBS" in ''|*[!0-9]*|0) ;; *)
        if [ "$JOBS" -gt "$_pw" ]; then
          _note "pool width: explicit DGN_TEST_JOBS=$JOBS is ABOVE the host's width $_pw ($_pwhy) -- honored as an operator choice (DGN-1878)."
        fi ;;
      esac
    else
      JOBS="$_pw"
      _note "pool width: $JOBS slot(s) from the host reading: $_pwhy (DGN-1878; DGN_TEST_JOBS overrides)"
    fi
    FAST_BUDGET="${DGN_FAST_BUDGET:-90}"; SLOW_BUDGET="${DGN_SLOW_BUDGET:-1200}"; PY_BUDGET=600
    ;;
  *) JOBS="${DGN_TEST_JOBS:-1}" ;;
esac
case "$JOBS" in ''|*[!0-9]*|0) _note "NOTE: bad DGN_TEST_JOBS='$JOBS' -- using 1"; JOBS=1 ;; esac

# ---- job table (DGN-1765) ---------------------------------------------------
# Pass 1 (below) CLASSIFIES every discovered suite in discovery order -- all
# lane / defer / manual / reuse accounting happens here, serially, exactly as
# before -- and queues the runnable ones as jobs. Pass 2 runs the queue on a
# pool JOBS wide. Pass 3 applies the verdict policy to each job's rc/output in
# the original discovery order, so the output does not depend on the width.
_JD="$_TMPD/jobs"; mkdir -p "$_JD"
_JIDX="$_JD/index"; : > "$_JIDX"   # "<idx> <kind> <serial-group|-> <relpath>"
_NJOB=0
_job_add() {  # <sh|py|pytest> <budget> <serial-group|-> <relpath>
  _NJOB=$((_NJOB + 1))
  printf '%s %s %s\n' "$1" "$2" "$4" > "$_JD/$_NJOB.spec"
  printf '%s %s %s %s\n' "$_NJOB" "$1" "$3" "$4" >> "$_JIDX"
}
_devtag_keep() {  # <suite|pytest> <relpath> -> rc 0 = verdict REUSED (counted, printed)
  [ "$LANE" = "devtag" ] || return 1
  if [ "$1" = pytest ]; then
    grep -qxF "$2" "$IMPACT_PY" && { _dt_run_why=affected; return 1; }
  else
    _impact_selected "$2" && { _dt_run_why=affected; return 1; }
  fi
  _dtv="$(_rec_row "$1" "$2")"
  case "$_dtv" in
    green) ;;
    red-known)
      if [ -z "$(_known_red_entry "$2")" ]; then _dt_run_why="quarantine-lapsed-since-record"; return 1; fi
      REUSED_RED_KNOWN=$((REUSED_RED_KNOWN + 1))
      ;;
    '') _dt_run_why="no-verdict-in-record"; return 1 ;;
    *)  _dt_run_why="record-verdict-$_dtv"; return 1 ;;
  esac
  REUSED=$((REUSED + 1))
  _note "REUSED: $2 $_dtv (full run $REUSE_BASE -- NOT re-run on this tree)"
  return 0
}
_devtag_ran() {  # <relpath> -- the honesty line for a re-run
  [ "$LANE" = "devtag" ] || return 0
  DT_RAN=$((DT_RAN + 1))
  _note "DEVTAG-RUN: $1 ($_dt_run_why)"
}

if [ "$LANE" = "flakecheck" ]; then
  # Only the record's red rows are queued; nothing is classified or counted.
  while read -r _fk _rel; do
    if [ "$_fk" = pytest ]; then
      _job_add pytest "$PY_BUDGET" - "$_rel"; continue
    fi
    _lane="$(sed -n '1,60p' "$ROOT/$_rel" 2>/dev/null | sed -nE 's/^#[[:space:]]*test-lane:[[:space:]]*([^[:space:]]*).*/\1/p' | head -1)"
    case "$_lane" in slow) _budget="$SLOW_BUDGET" ;; *) _budget="$FAST_BUDGET" ;; esac
    case "$_rel" in *.py) _job_add py "$_budget" - "$_rel" ;; *) _job_add sh "$_budget" - "$_rel" ;; esac
  done < "$_TMPD/flake.todo"
fi
if [ -s "$_MARKERS" ] && [ "$LANE" != "flakecheck" ]; then
  while IFS= read -r _rel; do
    [ -n "$_rel" ] || continue
    _f="$ROOT/$_rel"
    _h60="$(sed -n '1,60p' "$_f")"
    _mk="$(printf '%s\n' "$_h60" | grep -m1 -E '^#[[:space:]]*test-lane:' || true)"
    _lane="$(printf '%s' "$_mk" | sed -E 's/^#[[:space:]]*test-lane:[[:space:]]*//; s/[[:space:]].*$//')"
    _why="$(printf '%s' "$_mk" | sed -E 's/^#[[:space:]]*test-lane:[[:space:]]*[^[:space:]]*[[:space:]]*//')"
    # DGN-1765: '# test-serial: <group> <why>' -- same-group suites never overlap.
    _sgrp="$(printf '%s\n' "$_h60" | sed -nE 's/^#[[:space:]]*test-serial:[[:space:]]*([^[:space:]]+).*/\1/p' | head -1)"

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
        SUITE_FOUND=$((SUITE_FOUND + 1)); FAST_TOTAL=$((FAST_TOTAL + 1))
        _budget="$FAST_BUDGET"
        if [ "$LANE" = "affected" ] && ! _impact_selected "$_rel"; then
          SUITE_SKIPPED_IMPACT=$((SUITE_SKIPPED_IMPACT + 1))
          # DGN-1734: skipped is not green -- the last recorded verdict is
          # still printed/counted (the DGN-1508 read-back, fast lane).
          _lv="$(_ledger_entry "$_rel")"
          case "${_lv%% *}" in
            green) ;;
            red)
              if [ -n "$(_known_red_entry "$_rel")" ]; then
                SKIP_RED_KNOWN=$((SKIP_RED_KNOWN + 1))
              else
                SKIP_RED=$((SKIP_RED + 1))
                _note "SKIPPED-RED: $_rel RED per the ledger (red since ${_lv##* }), not selected by this push's impact set."
              fi
              ;;
            *) SKIP_UNMEASURED=$((SKIP_UNMEASURED + 1)) ;;
          esac
          continue
        fi
        _devtag_keep suite "$_rel" && continue
        SEL_FAST=$((SEL_FAST + 1))
        ;;
      slow)
        SUITE_FOUND=$((SUITE_FOUND + 1))
        _budget="$SLOW_BUDGET"
        # DGN-1734: an affected push never runs a slow suite (measured 501s /
        # 1503s pushes) -- a slow suite the push touches is DEFERRED like any
        # other, but named with its last ledger verdict so it cannot read green.
        if [ "$LANE" = "fast" ] || [ "$LANE" = "affected" ]; then
          SUITE_DEFER=$((SUITE_DEFER + 1))
          # DGN-1508: deferred, not re-verified -- but the LAST slow run's
          # verdict is a fact this push must still see as a number.
          _lv="$(_ledger_entry "$_rel")"
          if [ "$LANE" = "affected" ] && _impact_selected "$_rel"; then
            SEL_SLOW=$((SEL_SLOW + 1))
            case "${_lv%% *}" in
              green|red) _lvtxt="last ledger verdict ${_lv%% *} since ${_lv##* }" ;;
              *)         _lvtxt="no ledger verdict (unmeasured)" ;;
            esac
            if [ "$SEL_SLOW" -le 8 ]; then
              _note "DEFERRED-AFFECTED: $_rel is touched by this push but is SLOW -- NOT run on push; $_lvtxt."
            elif [ "$SEL_SLOW" -eq 9 ]; then
              _note "DEFERRED-AFFECTED: ... more affected slow suites follow, not listed (count in affected-slow-deferred=)."
            fi
          fi
          case "${_lv%% *}" in
            green) ;;
            red)
              if _void="$(_ledger_red_void "$_rel" slow "$_budget")"; then
                DEFER_VOID=$((DEFER_VOID + 1))
                _note "DEFERRED-VOID: $_rel ledger red (since ${_lv##* }) is not a slow-lane verdict -- $_void; verdict unknown until the next slow run."
              elif [ -n "$(_known_red_entry "$_rel")" ]; then
                DEFER_RED_KNOWN=$((DEFER_RED_KNOWN + 1))
              else
                DEFER_RED=$((DEFER_RED + 1))
                _note "DEFERRED-RED: $_rel RED per the last slow-lane run (red since ${_lv##* }), NOT in tests/KNOWN-RED."
                _note "        this push does not re-verify it; re-run: DGN_TEST_LANE=all bash git-hooks/run-tests.sh"
              fi
              ;;
            *)
              DEFER_UNMEASURED=$((DEFER_UNMEASURED + 1))
              ;;
          esac
          continue
        fi
        _devtag_keep suite "$_rel" && continue
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
    _devtag_ran "$_rel"
    case "$_f" in
      *.py) _job_add py "$_budget" "${_sgrp:--}" "$_rel" ;;
      *)    _job_add sh "$_budget" "${_sgrp:--}" "$_rel" ;;
    esac
    printf '%s\n' "$_lane" > "$_JD/$_NJOB.lane"   # ledger row tail (q1004)
  done < "$_MARKERS"
fi

# ---- PYTEST class -----------------------------------------------------------
# Structural, from the discovery pass above -- no path list to keep in sync
# with the tree (DGN-1267). agents/.template/memory-engine/tests was invisible
# to the old three-entry list and is picked up by this rule for free.
PY_ROOTS=()
while IFS= read -r _pr; do
  [ -n "$_pr" ] && PY_ROOTS+=("$ROOT/$_pr")
done < "$_PYROOTS"

if [ "${#PY_ROOTS[@]}" -gt 0 ] && [ "$LANE" != "flakecheck" ]; then
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
      if [ "$LANE" = "affected" ] && ! grep -qxF "$_rel" "$IMPACT_PY"; then
        SUITE_SKIPPED_IMPACT=$((SUITE_SKIPPED_IMPACT + 1))
        continue
      fi
      _devtag_keep pytest "$_rel" && continue
      _devtag_ran "$_rel"
      _job_add pytest "$PY_BUDGET" - "$_rel"
    done
  fi
fi

# ---- pass 2: run the queue (DGN-1765) ---------------------------------------
_run_job() {  # <idx> -> $_JD/<idx>.{out,rc,dur}
  read -r _jk _jb _jr < "$_JD/$1.spec"
  _jo="$_JD/$1.out"
  # Private TMPDIR per job, at every width (so JOBS=1 sees the same world).
  # Measured 2026-09-28: scripts/tests/test-d7-outbound-scope.sh counts
  # d7-diff.* in $TMPDIR before/after its own run and went red at 8 jobs
  # when a concurrent suite drove the same D7 gate; test-pack-install-tag-
  # tree.sh counts dogany-pack-tag.* the same way. Removed after each job.
  # DGN_TEST_KEEP_FAILED_TMP=1 keeps only failed jobs for diagnosis.
  if ! mkdir -p "$_JD/$1.tmp"; then
    echo 125 > "$_JD/$1.rc"
    return 0
  fi
  export TMPDIR="$_JD/$1.tmp"
  case "$_jk" in
    pytest)
      _jsh="$(_shared_home_for "pytest.$_jr")"   # DGN-1474
      DOGANY_SHARED_HOME="$_jsh" _RT_DUR="$_JD/$1.dur" _timed "$_jb" python3 -m pytest "$ROOT/$_jr" -q -rs >"$_jo" 2>&1
      ;;
    py)
      _jsh="$(_shared_home_for "$_jr")"          # DGN-1474: fresh hermetic shared home
      DOGANY_SHARED_HOME="$_jsh" _RT_DUR="$_JD/$1.dur" _timed "$_jb" python3 "$ROOT/$_jr" >"$_jo" 2>&1
      ;;
    *)
      _jsh="$(_shared_home_for "$_jr")"
      if [ "$HAVE_PY" -eq 1 ]; then
        DOGANY_SHARED_HOME="$_jsh" _RT_DUR="$_JD/$1.dur" _timed "$_jb" bash "$ROOT/$_jr" >"$_jo" 2>&1
      else
        # No python3 -> no timeout wrapper; run bare rather than not at all.
        _jt0="$(date +%s)"
        DOGANY_SHARED_HOME="$_jsh" bash "$ROOT/$_jr" >"$_jo" 2>&1
        _jrc=$?; echo "$(( $(date +%s) - _jt0 ))" > "$_JD/$1.dur"
        _finish_job_tmp "$1" "$_jrc"
        return 0
      fi
      ;;
  esac
  _jrc=$?
  _finish_job_tmp "$1" "$_jrc"
}
_finish_job_tmp() {
  local idx="$1" rc="$2" retained="${_TMPD}.failed-$1" failed=0 summary
  [ "$rc" -ne 0 ] && failed=1
  # Match the later pytest quiet-green verdict without changing its exit code.
  if [ "$_jk" = pytest ] && [ "$rc" -eq 0 ]; then
    summary="$(grep -E '^[0-9]+ .*(passed|failed|skipped|error|no tests ran)' "$_JD/$idx.out" | tail -1)"
    printf '%s' "$summary" | grep -qE '[0-9]+ passed' || failed=1
  fi
  if [ "$failed" -eq 1 ] && [ "${DGN_TEST_KEEP_FAILED_TMP:-0}" = 1 ]; then
    # Sibling of the runner root: its EXIT trap must not erase retained data.
    if mv "$TMPDIR" "$retained"; then
      echo "DRAFT en/ko: retained failed suite TMPDIR / 실패 테스트 임시 폴더 보존: $retained" >> "$_JD/$idx.out"
    else
      rc=125
    fi
  else
    chflags -R nouappend,nouchg "$TMPDIR" 2>/dev/null || true
    chmod -R u+w "$TMPDIR" 2>/dev/null || true
    rm -rf "$TMPDIR" || rc=125
  fi
  echo "$rc" > "$_JD/$idx.rc"
}
# Units: a job alone, or a whole serial group chained in discovery order.
# Longest estimated unit first (recorded durations; unknown = 60s), so the
# slowest suite never starts last.
_UNITS="$_TMPD/units"
awk -v df="${DUR_FILE:-/dev/null}" '
  BEGIN { while ((getline l < df) > 0) { split(l, a, " "); d[a[1]] = a[2] } }
  { e = ($4 in d) ? d[$4] : 60
    if ($3 == "-") { print e, $1; next }
    if (!($3 in ge)) order[++n] = $3
    ge[$3] += e; gi[$3] = gi[$3] (gi[$3] == "" ? "" : " ") $1 }
  END { for (i = 1; i <= n; i++) print ge[order[i]], gi[order[i]] }
' "$_JIDX" | LC_ALL=C sort -s -k1,1 -rn | cut -d' ' -f2- > "$_UNITS"
_POOL_T0="$(date +%s)"
if [ "$_NJOB" -gt 0 ]; then
  if [ "$JOBS" -gt 1 ] && mkfifo "$_TMPD/sem" 2>/dev/null; then
    _note "running $_NJOB job(s) on $JOBS parallel slot(s) (DGN-1765; DGN_TEST_JOBS=1 for serial)"
    exec 9<>"$_TMPD/sem"
    _n=0; while [ "$_n" -lt "$JOBS" ]; do printf x >&9; _n=$((_n + 1)); done
    while IFS= read -r _u; do
      read -r -n 1 _tok <&9
      ( for _i in $_u; do ( _run_job "$_i" ) 9>&-; done; printf x >&9 ) </dev/null &
    done < "$_UNITS"
    wait
    exec 9>&-
  else
    while IFS= read -r _u; do
      for _i in $_u; do ( _run_job "$_i" ) </dev/null; done
    done < "$_UNITS"
  fi
fi
_POOL_WALL=$(( $(date +%s) - _POOL_T0 ))

# ---- flake verdicts (DGN-1878): judged + recorded here, then the job index is
# emptied so the generic pass 3 below neither re-judges nor ledgers them.
if [ "$LANE" = "flakecheck" ]; then
  _fepoch="$(date +%s)"; _fdate="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  : > "$_TMPD/flake.rows"
  while read -r _idx _kind _sgrp _rel; do
    _rc="$(cat "$_JD/$_idx.rc" 2>/dev/null)"
    case "$_rc" in ''|*[!0-9]*) _rc=125 ;; esac
    _d="$(cat "$_JD/$_idx.dur" 2>/dev/null || echo -)"
    _fv=fail
    if [ "$_rc" -eq 0 ]; then
      _fv=pass
      # pytest: same QUIET-GREEN rule as the sweep -- exit 0 with no pass is no pass.
      if [ "$_kind" = pytest ] && ! grep -E '^[0-9]+ .*(passed|failed|skipped|error|no tests ran)' "$_JD/$_idx.out" | tail -1 | grep -qE '[0-9]+ passed'; then
        _fv=fail
      fi
    fi
    _rk=suite; [ "$_kind" = pytest ] && _rk=pytest
    printf 'flake %s %s %s %s %s %s rc=%s dur=%s\n' "$_rk" "$_rel" "$_fv" "$_START_HEAD" "$_fepoch" "$_fdate" "$_rc" "$_d" >> "$_TMPD/flake.rows"
    if [ "$_fv" = pass ]; then
      FLAKE_PASS=$((FLAKE_PASS + 1))
      _note "FLAKE-VERDICT: $_rel PASSED re-run alone (${_d}s) at $_fdate on ${_START_HEAD:0:12} -- recorded as a flake of the full run."
    else
      FLAKE_FAIL=$((FLAKE_FAIL + 1)); FAILS=$((FAILS + 1))
      _note "FAIL: $_rel FAILED again re-run alone (rc=$_rc) -- a REAL red, not a flake; the next dev cut stays full."
      tail -20 "$_JD/$_idx.out" >&2
    fi
  done < "$_JIDX"
  # Latest verdict per row replaces an older one; the record is rewritten whole.
  if { awk 'NR == FNR { seen[$2 " " $3] = 1; next } !($1 == "flake" && (($2 " " $3) in seen))' "$_TMPD/flake.rows" "$FULLRUN_REC" \
       && cat "$_TMPD/flake.rows"; } > "$FULLRUN_REC.tmp" 2>/dev/null && mv -f "$FULLRUN_REC.tmp" "$FULLRUN_REC" 2>/dev/null; then
    _note "flake verdicts recorded in the full-run record: pass=$FLAKE_PASS fail=$FLAKE_FAIL ($FULLRUN_REC)"
  else
    rm -f "$FULLRUN_REC.tmp" 2>/dev/null
    FAILS=$((FAILS + 1))
    _note "FAIL: full-run record NOT writable ($FULLRUN_REC) -- no flake verdict was recorded."
  fi
  : > "$_JIDX"
fi

# ---- pass 3: verdicts, in discovery order -----------------------------------
: > "$_TMPD/rec.rows"
while read -r _idx _kind _sgrp _rel; do
  [ "$_kind" = pytest ] && continue
  _sout="$_JD/$_idx.out"
  _rc="$(cat "$_JD/$_idx.rc" 2>/dev/null)"
  case "$_rc" in ''|*[!0-9]*) _rc=125; echo "[run-tests] job produced no exit status" >> "$_sout" ;; esac
  _budget="$(cut -d' ' -f2 "$_JD/$_idx.spec")"
  SUITE_RAN=$((SUITE_RAN + 1))
  _entry="$(_known_red_entry "$_rel")"
  _expired="$(_known_red_expired "$_rel")"
  [ -n "$_expired" ] && _note "NOTE: quarantine EXPIRED for $_rel ($_expired) -- it no longer shields this suite."

  _rv=green
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
    RED_KNOWN=$((RED_KNOWN + 1)); _rv=red-known
  elif [ "$_rc" -eq 124 ]; then
    _note "FAIL: suite TIMEOUT (>${_budget}s -- timeout = failure by design): $_rel"
    FAILS=$((FAILS + 1)); _rv=red
  else
    _note "FAIL: suite rc=$_rc: $_rel"
    tail -20 "$_sout" >&2
    FAILS=$((FAILS + 1)); _rv=red
  fi
  printf 'suite %s %s %s\n' "$_rel" "$_rv" "$(cat "$_JD/$_idx.dur" 2>/dev/null || echo -)" >> "$_TMPD/rec.rows"
  # DGN-1508/DGN-1625: record this suite's verdict for the ledger, on
  # every lane now, not just slow. since-date persists while the verdict
  # is unchanged; a transition resets it to today and is announced, so
  # newly-red never reads as already-red.
  if [ -n "$LEDGER" ]; then
    if [ "$_rc" -eq 0 ]; then _v=green; _cause=pass
    elif [ "$_rc" -eq 124 ]; then _v=red; _cause="timeout@${_budget}s"
    else _v=red; _cause=fail; fi
    _jlane="$(cat "$_JD/$_idx.lane" 2>/dev/null)"
    _lv="$(_ledger_entry "$_rel")"
    _prev_v="${_lv%% *}"
    _since="$_TODAY"
    if [ "$_prev_v" = "$_v" ] && [ -n "${_lv##* }" ]; then _since="${_lv##* }"; fi
    # A streak measured on another lane is not this lane's streak (q1004).
    _pm="$(_ledger_meta "$_rel")"
    if [ -n "$_pm" ] && [ -n "$_jlane" ] && [ "${_pm%% *}" != "$_jlane" ]; then _since="$_TODAY"; fi
    if [ "$_v" = "red" ]; then
      if [ "$_prev_v" = "green" ]; then
        _note "NEWLY RED: $_rel was green on the last recorded run (since ${_lv##* }) -- red observed from $_TODAY."
      elif [ -z "$_prev_v" ]; then
        _note "FIRST-MEASURED RED: $_rel has no ledger history -- red observed from $_TODAY, true onset unknown."
      fi
    fi
    printf '%s %s %s lane=%s cause=%s\n' "$_rel" "$_v" "$_since" "${_jlane:-unknown}" "$_cause" >> "$_TMPD/ledger.new"
  fi
done < "$_JIDX"

if [ -s "$_MARKERS" ]; then
  # Record the verdict of this slow run into the clock.
  if { [ "$LANE" = "slow" ] || [ "$LANE" = "all" ]; } && [ -n "$STAMP" ]; then
    if [ "$FAILS" -eq 0 ]; then
      _stamp_put "$STAMP" green \
        && _note "slow lane green -- staleness clock reset ($STAMP)"
    else
      _stamp_put "$STAMP" red \
        && _note "slow lane RED -- clock recorded verdict=red ($STAMP); this push is blocked by the failures above."
    fi
  fi

  # DGN-1625: install the per-suite ledger atomically, on every lane (this
  # used to be gated to non-fast runs, back when only slow suites were
  # recorded; a fast run visits only fast suites, so a whole-file replace
  # here would silently drop every other lane's history -- _ledger_merge_into
  # carries forward whatever this run did not visit instead).
  if [ -n "$LEDGER" ] && [ "$STAMP_WRITE" -eq 1 ] && [ -f "$_TMPD/ledger.new" ]; then
    if _ledger_merge_into > "$LEDGER.tmp" 2>/dev/null \
       && mv -f "$LEDGER.tmp" "$LEDGER" 2>/dev/null; then
      _note "test ledger updated: $(awk '$2=="red"{n++} END{print n+0}' "$LEDGER") red / $(wc -l < "$LEDGER" | tr -d ' ') suites ($LEDGER)"
    else
      rm -f "$LEDGER.tmp" 2>/dev/null
      _note "NOTE: slow-lane ledger NOT writable ($LEDGER) -- deferred-red counting will read stale/absent data."
    fi
  fi
fi

while read -r _idx _kind _sgrp _rel; do
  [ "$_kind" = pytest ] || continue
  _out="$_JD/$_idx.out"
  _rc="$(cat "$_JD/$_idx.rc" 2>/dev/null)"
  case "$_rc" in ''|*[!0-9]*) _rc=125 ;; esac
  PY_RAN=$((PY_RAN + 1))
  # Final pytest summary line ("N failed, M passed, ... in Xs").
  _sum="$(grep -E '^[0-9]+ .*(passed|failed|skipped|error|no tests ran)' "$_out" | tail -1)"
  [ -z "$_sum" ] && _sum="$(tail -1 "$_out")"
  _rv=red
  if [ "$_rc" -eq 124 ]; then
    _note "FAIL: pytest TIMEOUT (>${PY_BUDGET}s -- timeout = failure by design): $_rel"
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
    _rv=green
    _note "$_rel: $_sum"
    # Partial skips pass, but never silently (DGN-1154).
    if printf '%s' "$_sum" | grep -qE '[0-9]+ skipped'; then
      _note "NOTE: skipped tests present in $_rel -- skips are surfaced, not counted as pass."
    fi
  fi
  printf 'pytest %s %s %s\n' "$_rel" "$_rv" "$(cat "$_JD/$_idx.dur" 2>/dev/null || echo -)" >> "$_TMPD/rec.rows"
done < "$_JIDX"

# ---- per-suite durations (DGN-1765) -----------------------------------------
if [ "$_NJOB" -gt 0 ]; then
  _dsum="$(awk '$4 != "-" { s += $4 } END { printf "%d", s }' "$_TMPD/rec.rows")"
  _note "timing: $_NJOB job(s), jobs=$JOBS, pool wall ${_POOL_WALL}s, suite-seconds ${_dsum}s; slowest: $(awk '$4 != "-"' "$_TMPD/rec.rows" | LC_ALL=C sort -k4,4 -rn | head -5 | awk '{printf "%s%s %ss", (NR > 1 ? ", " : ""), $2, $4}')"
  if [ -n "$DUR_FILE" ] && [ "$STAMP_WRITE" -eq 1 ] && [ "$LANE" != "flakecheck" ]; then
    awk -v t="$_TODAY" '$4 != "-" { print $2, $4, t }' "$_TMPD/rec.rows" > "$_TMPD/dur.new"
    {
      if [ -f "$DUR_FILE" ]; then
        awk 'NR == FNR { seen[$1] = 1; next } !($1 in seen)' "$_TMPD/dur.new" "$DUR_FILE" \
          | while IFS= read -r _dl; do [ -e "$ROOT/${_dl%% *}" ] && printf '%s\n' "$_dl"; done
      fi
      cat "$_TMPD/dur.new"
    } > "$DUR_FILE.tmp" 2>/dev/null && mv -f "$DUR_FILE.tmp" "$DUR_FILE" 2>/dev/null \
      || { rm -f "$DUR_FILE.tmp" 2>/dev/null; _note "NOTE: durations file NOT writable ($DUR_FILE)"; }
  fi
fi

# ---- S3 launchd domain guard (DGN-1572), continued: compare vs baseline ----
_launchd_snapshot "$_LAUNCHD_AFTER"
_LAUNCHD_NEW="$(comm -13 "$_LAUNCHD_BEFORE" "$_LAUNCHD_AFTER" 2>/dev/null)"
if [ -n "$_LAUNCHD_NEW" ]; then
  FAILS=$((FAILS + 1))
  _note "FAIL: this run registered NEW unit(s) in the owner's live launchd domain (DGN-1572 S3)."
  printf '%s\n' "$_LAUNCHD_NEW" | while IFS= read -r _lbl; do
    [ -n "$_lbl" ] && _note "      NEW LABEL: $_lbl"
  done
  _note "      this assertion is READ-ONLY (launchctl list) and removed nothing. A test somewhere"
  _note "      bypassed the DOGANY_LAUNCH_AGENTS_DIR redirect -- find and fix it; do not just bootout the label."
fi

# ---- verdict (a RESULT line prints on EVERY path -- silence is never success)
_result="lane=$LANE suites=$SUITE_RAN/$SUITE_FOUND deferred-slow=$SUITE_DEFER manual=$SUITE_MANUAL"
_result="$_result deferred-red=$DEFER_RED deferred-red-known=$DEFER_RED_KNOWN deferred-unmeasured=$DEFER_UNMEASURED deferred-void=$DEFER_VOID"
_result="$_result unlaned=$SUITE_UNLANED known-red=$RED_KNOWN stale-quarantine=$RED_STALE lib=$SUITE_LIB"
_result="$_result pytest-roots=$PY_RAN/$PY_FOUND failures=$FAILS degraded=$DEGRADED"
_result="$_result selected=$SEL_FAST/$FAST_TOTAL affected-slow-deferred=$SEL_SLOW skipped-impact=$SUITE_SKIPPED_IMPACT"
_result="$_result skipped-red=$SKIP_RED skipped-unmeasured=$SKIP_UNMEASURED fallback=$IMPACT_FALLBACK full-fast-age=$FULLFAST_AGE"
_result="$_result jobs=$JOBS reused=$REUSED reused-red-known=$REUSED_RED_KNOWN reuse-base=$REUSE_BASE"
_result="$_result heavy-lock=${HEAVY_LOCK%%(*} flake-cleared=$FLAKE_CLEARED"
[ "$LANE" = "flakecheck" ] && _result="$_result flake-pass=$FLAKE_PASS flake-fail=$FLAKE_FAIL"
# DGN-1734: the full-fast clock records the verdict of a run that executed
# every fast suite (the attempt was stamped before the loop).
if [ "$LANE" != "affected" ] && [ "$LANE" != "slow" ] && [ "$LANE" != "devtag" ] && [ "$LANE" != "flakecheck" ] && [ -n "$FULLFAST_STAMP" ] && [ -f "$FULLFAST_STAMP" ] && [ -s "$_MARKERS" ]; then
  if [ "$FAILS" -eq 0 ]; then _stamp_put "$FULLFAST_STAMP" green; else _stamp_put "$FULLFAST_STAMP" red; fi
fi
# DGN-1765: every lane=all run from the primary checkout REPLACES the full-run
# record -- a red or dirty-tree run too, so a dev tag never reuses verdicts
# older than the newest full run. Written whole, then moved into place.
if [ "$LANE" = "all" ] && [ -n "$FULLRUN_REC" ] && [ "$STAMP_WRITE" -eq 1 ] && [ -s "$_MARKERS" ]; then
  {
    echo "# dogany full-run record (DGN-1765) -- written by git-hooks/run-tests.sh lane=all; read by lane=devtag"
    echo "commit $_START_HEAD"
    echo "clean $_START_CLEAN"
    echo "epoch $(date +%s)"
    echo "date $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "failures $FAILS"
    echo "degraded $DEGRADED"
    echo "jobs $JOBS"
    echo "wall $_POOL_WALL"
    cat "$_TMPD/rec.rows"
  } > "$FULLRUN_REC.tmp" 2>/dev/null && mv -f "$FULLRUN_REC.tmp" "$FULLRUN_REC" 2>/dev/null \
    && _note "full-run record written: failures=$FAILS commit=${_START_HEAD:0:12} clean=$_START_CLEAN ($FULLRUN_REC)" \
    || { rm -f "$FULLRUN_REC.tmp" 2>/dev/null; _note "NOTE: full-run record NOT writable ($FULLRUN_REC) -- the next dev tag runs the full lane."; }
fi
if [ "$FAILS" -gt 0 ]; then
  _note "RESULT: FAIL -- $_result"
  exit 1
fi
if [ "$LANE" = "flakecheck" ]; then
  _note "RESULT: FLAKE-RECORDED -- $_result -- every red row of full run ${_rc_commit:0:12} passed re-run alone;"
  _note "        the next dev tag (< ${DEV_REUSE_MAX_AGE_H}h of that run) reuses it and re-runs failed + affected. A stable tag still runs everything."
  exit 0
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
if [ "$SUITE_DEFER" -gt 0 ] || [ "$SUITE_MANUAL" -gt 0 ] || [ "$RED_KNOWN" -gt 0 ] || [ "$RED_STALE" -gt 0 ] \
   || [ "$SUITE_SKIPPED_IMPACT" -gt 0 ] || [ "$REUSED" -gt 0 ]; then
  _note "RESULT: PASS-WITH-GAPS -- $_result"
  _note "        PASS covers only what RAN. $SUITE_DEFER slow-lane suite(s) deferred, $SUITE_MANUAL manual,"
  [ "$LANE" = "devtag" ] && _note "        dev tag: $DT_RAN suite(s)/pytest root(s) RAN on this tree (DEVTAG-RUN lines), $REUSED verdict(s) REUSED from full run $REUSE_BASE ($REUSED_RED_KNOWN of them red-known) and NOT re-run (REUSED lines; DGN-1765),"
  [ "$SEL_SLOW" -gt 0 ] && _note "        $SEL_SLOW slow suite(s) touched by this push were NOT run (DGN-1734: they run at the tag / staleness clock, or now via the full sweep below; see DEFERRED-AFFECTED above),"
  [ "$SUITE_SKIPPED_IMPACT" -gt 0 ] && _note "        $SUITE_SKIPPED_IMPACT fast suite(s)/pytest root(s) skipped by impact selection (DGN-1734; full fast every ${FULL_FAST_MAX_AGE_H}h + at every tag),"
  _note "        $RED_KNOWN known-red (quarantined), $RED_STALE stale quarantine entr(ies). Full sweep:"
  _note "        DGN_TEST_LANE=all bash git-hooks/run-tests.sh"
  if [ "$DEFER_RED" -gt 0 ]; then
    _note "        DEFERRED-RED: $DEFER_RED deferred suite(s) RED per the last slow-lane run and NOT quarantined"
    _note "        (DGN-1508 -- see the DEFERRED-RED lines above for which and since when)."
  fi
  exit 0
fi
_note "RESULT: PASS -- $_result"
exit 0
