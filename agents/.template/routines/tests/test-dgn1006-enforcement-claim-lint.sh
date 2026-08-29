#!/usr/bin/env bash
# test-dgn1006-enforcement-claim-lint.sh -- enforcement-claim-lint coverage.
#
# DGN-1006 (carried to the canonical template by DGN-1079-group2-carryback):
# governing docs asserting enforcement machinery that does not exist.
# All fixtures below are synthetic/self-contained (no dependency on this
# repo's own git history or specific commit SHAs, so the suite is portable
# across any instance the template is minted into):
#   A1. A synthetic pre-correction-style snippet (main-guard claim naming an
#       unwired env flag, plus a gap-gate BLOCK claim with no implementing
#       script) MUST be flagged.
#   A2. The CURRENT template governing-doc set (as shipped) MUST come back
#       clean of enforcement-claim violations.
#   A3. Honest unbuilt markers ([미구현 ...], not built, RETIRED) suppress.
#   A4. Correction prose ("was NEVER BUILT", "the old rule said") never trips.
#   A5. A claim naming a real+wired artifact passes.
#   A6. A claim naming a real-but-unwired / missing artifact is flagged.
#
# Run: bash routines/tests/test-dgn1006-enforcement-claim-lint.sh
# Exit: 0 all pass, nonzero any fail.
#
# Safe/offline: no push, no claude spawn. Writes scratch fixtures only.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
LINT="$REPO/routines/enforcement-claim-lint.sh"

PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dgn1006-test.XXXXXX)"
cleanup() { rm -rf "$TDIR"; }
trap cleanup EXIT

# ===========================================================================
# A1. Synthetic "false enforcement-claim" catch -- the DGN-1005-class defect
#     shape: indicative-mood prose naming an env flag + a script, citing a
#     design ticket, where neither actually verifies.
# ===========================================================================
echo "== A1: synthetic false-enforcement-claim text must be flagged"
HIST="$TDIR/hist-agentmd.md"
cat > "$HIST" << 'EOF'
### Infra propagation
- Baseline infra/framework change: branch work only; canonical main advances via release.sh only -- no direct main merge; pre-commit hook blocks bare main commits (DGN-9001).
- Public mirror: roll-forward only. Gap gate: gap>=1 WARN / gap>=2 or migration BLOCK step-through (DGN-9002).

### Local commit checkpoint
- canonical main = release.sh path only; branch work is free. Pre-commit hook blocks direct main commits (DOGANY_RELEASE_FAKE_FLAG=1 bypasses -- DGN-9001).
EOF
OUT="$("$LINT" "$HIST" 2>&1)"; RC=$?
[ "$RC" = "1" ] && ok "synthetic text exits 1" || bad "synthetic text exit=$RC (want 1)"
echo "$OUT" | grep -q "DOGANY_RELEASE_FAKE_FLAG" \
  && ok "main-guard claim flagged (env flag absent from any wired hook)" \
  || bad "main-guard claim NOT flagged"
echo "$OUT" | grep -q "verdict-token claim" \
  && ok "gap-gate BLOCK claim flagged (no implementing script)" \
  || bad "gap-gate BLOCK claim NOT flagged"

# ===========================================================================
# A2. Current shipped template governing docs -- known-residual tolerant.
#     NOT asserting global RC==0: unrelated doc drift over time would make
#     this brittle. Two pre-existing findings are known and out of scope for
#     this lint's own carryback (release-closer.md / dogany-relogin-rebind
#     SKILL.md reference release.sh / token-sync.sh, which live in this
#     repo's own top-level scripts//skills/ dirs -- outside agents/.template
#     -- a pre-mint layering quirk, not a lint defect). Any OTHER violation
#     is new and must fail this test.
# ===========================================================================
echo "== A2: current template governing docs -- no NEW violations"
OUT="$("$LINT" 2>&1)"; RC=$?
KNOWN_RESIDUAL='release-closer\.md:12|dogany-relogin-rebind/SKILL\.md:18'
UNEXPECTED="$(echo "$OUT" | grep -E '^/.*:[0-9]+$' | grep -Ev "$KNOWN_RESIDUAL" || true)"
if [ "$RC" = "0" ]; then
  ok "default scan exits 0 (clean)"
elif [ -z "$UNEXPECTED" ]; then
  ok "default scan has only the 2 known pre-existing residual findings (out of scope)"
else
  bad "default scan has UNEXPECTED violation(s) beyond the known residual"
  echo "$UNEXPECTED" | sed 's/^/    /'
fi

# ===========================================================================
# A3. Unbuilt-marker suppression
# ===========================================================================
echo "== A3: honest unbuilt markers suppress"
M1="$TDIR/marker-inline.md"
cat > "$M1" << 'EOF'
- Deployment gate: promote.sh --check blocks promotion to stable. [미구현: promote.sh absent, ring runs single-track today]
EOF
"$LINT" "$M1" > /dev/null 2>&1 && ok "inline [미구현] marker suppresses" || bad "inline [미구현] marker did not suppress"

M2="$TDIR/marker-filetop.md"
cat > "$M2" << 'EOF'
# SOME-SOT -- target model

> [미구현: 채널 machinery phase 0~2 진행중]
> 아래는 목표 세계관이며 현재 가동 상태가 아니다.

- promote.sh gate blocks promotion between rings; the pre-commit hook blocks direct main commits.
EOF
"$LINT" "$M2" > /dev/null 2>&1 && ok "file-top blockquote [미구현] suppresses whole file" || bad "file-top marker did not suppress"

M3="$TDIR/marker-prevline.md"
cat > "$M3" << 'EOF'
[미구현: hook not built yet -- design only]
- The nonexistent-guard.sh pre-push hook blocks secret leaks (DGN-000).
EOF
"$LINT" "$M3" > /dev/null 2>&1 && ok "marker on preceding line suppresses" || bad "preceding-line marker did not suppress"

M4="$TDIR/marker-retired.md"
cat > "$M4" << 'EOF'
- Gap gate: WARN-only telemetry (gap>=1 WARN; BLOCK retired by DGN-880) -- safety is structural.
EOF
"$LINT" "$M4" > /dev/null 2>&1 && ok "'retired by' on the claim line suppresses" || bad "'retired by' did not suppress"

# ===========================================================================
# A4. Correction prose must not trip
# ===========================================================================
echo "== A4: correction/history prose never trips"
C1="$TDIR/correction.md"
cat > "$C1" << 'EOF'
# postmortem notes
- The claimed pre-commit hook that blocks bare main commits was NEVER BUILT (false close).
- The old rule said the gap gate BLOCKs step-through publishes; that was misinformation.
- scripts/hooks/pre-commit 신설은 존재한 적 없음 -- 문서가 없는 기계를 있다고 서술했다.

> quoted historical text: "pre-commit hook blocks direct main commits (DOGANY_RELEASE=1 bypasses)"
EOF
"$LINT" "$C1" > /dev/null 2>&1 && ok "correction prose + quoted history clean" || bad "correction prose tripped the lint"

# Wrapped bullet: marker on the bullet's first line, claim tokens on line 3
C2="$TDIR/wrapped.md"
cat > "$C2" << 'EOF'
- Gap gate: WARN-only telemetry (the BLOCK is RETIRED). Safety is
  structural: the mirror-forward publisher steps one tag at a time (ascending), so the
  multi-version straight-jump the old BLOCK guarded against cannot occur.
EOF
"$LINT" "$C2" > /dev/null 2>&1 && ok "wrapped bullet with RETIRED on first line clean" || bad "wrapped-bullet continuation tripped"

# ===========================================================================
# A5. Real + wired artifact passes
# ===========================================================================
echo "== A5: real+wired artifact passes"
W1="$TDIR/wired.md"
cat > "$W1" << 'EOF'
- Usage window: PreToolUse hook (`routines/usage-gate.py`) mechanically gates heavy dispatches. Denies above threshold.
EOF
"$LINT" "$W1" > /dev/null 2>&1 && ok "wired usage-gate script passes" || bad "real+wired artifact was flagged"

# ===========================================================================
# A6. Real-but-unwired / missing artifacts flagged
# ===========================================================================
echo "== A6: unwired / missing artifacts flagged"

# (a) git-hook claim against a repo whose hook lacks the claimed behavior
FR="$TDIR/fakerepo"
git init -q "$FR"
mkdir -p "$FR/.git/hooks"
printf '#!/bin/sh\nexit 0\n' > "$FR/.git/hooks/pre-commit"
chmod +x "$FR/.git/hooks/pre-commit"
U1="$TDIR/unwired-hook.md"
cat > "$U1" << 'EOF'
- Pre-commit hook blocks direct main commits (arms on `.dogany-canonical` marker).
EOF
OUT="$(ECL_HOOK_REPOS="$FR" "$LINT" "$U1" 2>&1)"; RC=$?
{ [ "$RC" = "1" ] && echo "$OUT" | grep -q "absent from its non-comment lines"; } \
  && ok "hook exists but lacks claimed behavior -> flagged" \
  || bad "content-less hook not flagged (exit=$RC)"

# (b) claude-code hook script exists but is not wired in settings
FROOT="$TDIR/fakeroot"
mkdir -p "$FROOT/routines"
printf '#!/usr/bin/env python3\n' > "$FROOT/routines/foo-gate.py"
: > "$TDIR/empty-settings.json"
U2="$TDIR/unwired-settings.md"
cat > "$U2" << 'EOF'
- PreToolUse hook (routines/foo-gate.py) mechanically blocks oversized dispatches.
EOF
OUT="$(ECL_ROOT="$FROOT" ECL_HOOK_REPOS="$FROOT" ECL_SCRIPT_DIRS="$FROOT/routines" \
      ECL_SETTINGS="$TDIR/empty-settings.json" "$LINT" "$U2" 2>&1)"; RC=$?
{ [ "$RC" = "1" ] && echo "$OUT" | grep -q "not referenced in .claude/settings"; } \
  && ok "present-but-unwired claude hook -> flagged" \
  || bad "unwired claude hook not flagged (exit=$RC)"

# (c) named script does not exist anywhere
U3="$TDIR/missing-script.md"
cat > "$U3" << 'EOF'
- Release ring: promote-ring-gate.sh blocks promotion to stable without owner approval.
EOF
OUT="$("$LINT" "$U3" 2>&1)"; RC=$?
{ [ "$RC" = "1" ] && echo "$OUT" | grep -q "not found"; } \
  && ok "missing script -> flagged" \
  || bad "missing script not flagged (exit=$RC)"

# (d) verdict-token claim with no implementing machinery
U4="$TDIR/verdict.md"
cat > "$U4" << 'EOF'
- Frobnicate gate: drift>=2 or zorblax BLOCK step-through (DGN-9003).
EOF
OUT="$("$LINT" "$U4" 2>&1)"; RC=$?
{ [ "$RC" = "1" ] && echo "$OUT" | grep -q "verdict-token claim"; } \
  && ok "unimplemented verdict-token claim -> flagged" \
  || bad "verdict-token claim not flagged (exit=$RC)"

# (e) noun-form "<script> gate" naming a missing script
U5="$TDIR/gate-noun.md"
cat > "$U5" << 'EOF'
- promote.sh --check gate criteria: canary consumed + surfaces exercised + bridge alive + owner approval.
EOF
OUT="$("$LINT" "$U5" 2>&1)"; RC=$?
{ [ "$RC" = "1" ] && echo "$OUT" | grep -q "not found"; } \
  && ok "noun-form gate claim on missing script -> flagged" \
  || bad "noun-form gate claim not flagged (exit=$RC)"

# (f) noun-form design prose ("needs a check") with a REAL script must pass --
#     wiring is only demanded of indicative enforcement verbs
U6="$TDIR/gate-design.md"
cat > "$U6" << 'EOF'
1. Send-time confirmation gate: out-of-band dispatch path (push.sh / cron / proactive) needs a check -- approved surface types only.
EOF
"$LINT" "$U6" > /dev/null 2>&1 && ok "noun-form design note with real script passes" \
  || bad "noun-form design note wrongly flagged"

# ===========================================================================
# Exit-code contract
# ===========================================================================
echo "== exit codes"
"$LINT" "$TDIR/does-not-exist.md" > /dev/null 2>&1
[ $? = 2 ] && ok "missing target file exits 2" || bad "missing target file wrong exit"
"$LINT" --list > /dev/null 2>&1
[ $? = 0 ] && ok "--list exits 0" || bad "--list wrong exit"

# ===========================================================================
echo ""
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ] || exit 1
exit 0
