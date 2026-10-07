#!/usr/bin/env bash
# test-lane: fast
# routines/tests/test-dgn1504-write-isolation.sh -- DGN-1504 write-mode
# isolation in dispatch-detached.sh, model-free.
#
# Fixture estate: a canonical-shaped repo (main + dev, .dogany-canonical,
# a linked sibling worktree), a second repo, the workspace repo, a plain dir,
# and another dispatch's worktree under WORKTREE_BASE. The real
# provision_worktree cuts this run's worktree; the real renderer builds the
# write profile; the profile is judged by fixtures/sandbox_exec_model.py
# (seatbelt last-match-wins model) so the suite runs on any host. On macOS
# the REAL kernel block at the end proves the same verdicts with
# sandbox-exec itself (skipped elsewhere, printed as SKIP).
# Exit 0 = all pass.
set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
SBMODEL="$HARNESS_DIR/fixtures/sandbox_exec_model.py"
DISPATCH_TEST=1 source "$REPO/routines/dispatch-detached.sh"
set +e

PASS=0; FAIL=0; SKIP=0
ok()   { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$1"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (got: $2, want: $3)"; fi; }
has()  { if grep -qF -- "$3" <<<"$2"; then ok "$1"; else bad "$1 (missing: $3)"; fi; }
lacks(){ if grep -qF -- "$3" <<<"$2"; then bad "$1 (unexpected: $3)"; else ok "$1"; fi; }

export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
unset DOGANY_ESTATE_ROOTS

LAB="$(mktemp -d "${TMPDIR:-/tmp}/dgn1504-test.XXXXXX")"
LAB="$(cd "$LAB" && pwd -P)"
cleanup() { rm -rf "$LAB"; }
trap cleanup EXIT

# Stand-in sandbox-exec (profile model) first on PATH.
mkdir -p "$LAB/bin"
printf '#!/bin/sh\nexec python3 "%s" exec "$@"\n' "$SBMODEL" > "$LAB/bin/sandbox-exec"
chmod +x "$LAB/bin/sandbox-exec"
REAL_SANDBOX="$(command -v sandbox-exec 2>/dev/null || true)"
# A run that is itself inside a seatbelt (e.g. a DGN-1504 dispatch worker)
# cannot apply a nested profile (sandbox_apply: EPERM): every "denied"
# verdict would be vacuous there, so the real-kernel blocks are SKIPPED.
if [[ -n "$REAL_SANDBOX" ]]; then
  _kprobe="$(mktemp "${TMPDIR:-/tmp}/dgn1504-kprobe.XXXXXX")"
  echo '(version 1) (allow default)' > "$_kprobe"
  "$REAL_SANDBOX" -f "$_kprobe" /usr/bin/true 2>/dev/null || REAL_SANDBOX=""
  rm -f "$_kprobe"
fi
PATH="$LAB/bin:$PATH"; hash -r

mk_repo() {
  git init -q -b main "$1"
  echo "$2" > "$1/README"
  git -C "$1" add -A && git -C "$1" commit -qm init
}
EST="$LAB/estate"
CANON="$EST/canon"; OTHER="$EST/other"; WSR="$EST/ws"
mkdir -p "$EST/plain"
mk_repo "$CANON" canon
touch "$CANON/.dogany-canonical"; git -C "$CANON" add -A; git -C "$CANON" commit -qm marker
git -C "$CANON" branch dev
git -C "$CANON" checkout -q dev
mk_repo "$OTHER" other
mk_repo "$WSR" ws
git -C "$CANON" worktree add -q "$EST/.wt/sib" -b sib dev
WORKTREE_BASE="$LAB/wtbase"; mkdir -p "$WORKTREE_BASE"
git -C "$CANON" worktree add -q "$WORKTREE_BASE/dsp-other-x" -b dsp/x dev

# This run: worktree cut from canon by the real provisioner.
WORKSPACE="$WSR"
RUNID="dsp-test-0001"; RUNDIR="$LAB/run"; mkdir -p "$RUNDIR"
WT_SLUG="t1"; WT_REPO="$CANON"; WT_BASE_ARG=""
provision_worktree; check "P0 provision_worktree" "$?" "0"
check "P0b base resolved to dev (canonical marker)" "$WT_BASE" "dev"
OWN="$(cd "$WT_PATH" && pwd -P)"
GD="$(wt_common_dir "$CANON")"

# --- roster -----------------------------------------------------------------
write_roster
ROSTER="$(printf '%s\n' "${ROSTER_CHECKOUTS[@]}")"
has   "R1 roster: canonical main checkout"        "$ROSTER" "$CANON"
has   "R2 roster: linked sibling worktree"        "$ROSTER" "$EST/.wt/sib"
has   "R3 roster: sibling repo"                   "$ROSTER" "$OTHER"
has   "R4 roster: workspace"                      "$ROSTER" "$WSR"
lacks "R5 roster: own worktree excluded"          "$ROSTER" "$OWN"
lacks "R6 roster: WORKTREE_BASE runs not listed"  "$ROSTER" "dsp-other-x"
lacks "R7 roster: non-repo dir excluded"          "$ROSTER" "$EST/plain"
check "R8 roster: one git dir per repo"           "${#ROSTER_GITDIRS[@]}" "3"
( DOGANY_ESTATE_ROOTS="$LAB/nowhere"; write_roster; printf '%s\n' "${ROSTER_CHECKOUTS[@]}" ) > "$LAB/r9"
lacks "R9 DOGANY_ESTATE_ROOTS override narrows the scan" "$(cat "$LAB/r9")" "$OTHER"

# --- profile verdicts ---------------------------------------------------------
PROF="$RUNDIR/write.sb"
render_write_profile "$PROF"; check "V0 render" "$?" "0"
v() { python3 "$SBMODEL" verdict "$PROF" 'file-write*' "$1"; }
check "V1 own worktree file -> allow"                  "$(v "$OWN/src/x")" "allow"
check "V2 canonical main checkout -> deny"             "$(v "$CANON/scripts/mint.sh")" "deny"
check "V3 sibling linked worktree -> deny"             "$(v "$EST/.wt/sib/README")" "deny"
check "V4 sibling repo -> deny"                        "$(v "$OTHER/README")" "deny"
check "V5 workspace -> deny"                           "$(v "$WSR/.telegram_bot/x")" "deny"
check "V6 another dispatch's worktree -> deny"         "$(v "$WORKTREE_BASE/dsp-other-x/README")" "deny"
check "V7 object store -> allow (commits)"             "$(v "$GD/objects/ab/cdef")" "allow"
check "V8 own branch ref -> allow"                     "$(v "$GD/refs/heads/dsp/t1")" "allow"
check "V9 dev ref -> deny"                             "$(v "$GD/refs/heads/dev")" "deny"
check "V9b dev ref lock -> deny"                       "$(v "$GD/refs/heads/dev.lock")" "deny"
check "V9c main ref -> deny"                           "$(v "$GD/refs/heads/main")" "deny"
check "V9d a branch merely prefixed dev -> allow"      "$(v "$GD/refs/heads/devx")" "allow"
check "V10 main checkout index -> deny"                "$(v "$GD/index")" "deny"
check "V10b main checkout HEAD (branch switch) -> deny" "$(v "$GD/HEAD")" "deny"
check "V10c repo config -> deny"                       "$(v "$GD/config")" "deny"
check "V10d packed-refs -> deny"                       "$(v "$GD/packed-refs")" "deny"
check "V10e hooks -> deny"                             "$(v "$GD/hooks/pre-commit")" "deny"
ADMIN="$(git -C "$WT_PATH" rev-parse --git-dir)"; ADMIN="$(cd "$ADMIN" && pwd -P)"
check "V11 own worktree admin dir (index/HEAD) -> allow" "$(v "$ADMIN/index")" "allow"
check "V12 unrelated path -> allow (scope is checkouts only)" "$(v "$LAB/elsewhere/f")" "allow"
check "V13 new worktree beside the estate -> allow (git worktree add)" "$(v "$EST/.wt/new/README")" "allow"
tail -1 "$PROF" | grep -qF "$OWN"; check "V14 own worktree allow is the LAST rule" "$?" "0"

# --- self-check ---------------------------------------------------------------
write_profile_selfcheck "$PROF" >/dev/null 2>&1; check "S1 self-check passes a sound profile" "$?" "0"
sed '/OWN WORKTREE LAST/,$d' "$PROF" > "$RUNDIR/closed.sb"
write_profile_selfcheck "$RUNDIR/closed.sb" >/dev/null 2>&1; check "S2 self-check refuses a profile that closes the own worktree" "$?" "1"
SAVED=("${ROSTER_CHECKOUTS[@]}"); ROSTER_CHECKOUTS=()
render_write_profile "$RUNDIR/open.sb" >/dev/null 2>&1
ROSTER_CHECKOUTS=("${SAVED[@]}")
write_profile_selfcheck "$RUNDIR/open.sb" >/dev/null 2>&1; check "S3 self-check refuses a profile that leaves a shared checkout open" "$?" "1"
echo '(version 1' > "$RUNDIR/syntax.sb"
write_profile_selfcheck "$RUNDIR/syntax.sb" >/dev/null 2>&1; check "S4 self-check refuses a profile that does not compile" "$?" "1"
check "S5 no probe residue left in any checkout" "$(find "$EST" "$OWN" -name '.write-probe-*' | wc -l | tr -d ' ')" "0"
( ROSTER_CHECKOUTS=("$LAB/bad\"q"); render_write_profile "$RUNDIR/q.sb" 2>/dev/null ); check "S6 render refuses an unquotable roster path" "$?" "1"

# --- worker policy ------------------------------------------------------------
WRITE_PROFILE="$PROF"; SANDBOX_PROFILE=""; SESSION_ID=""; select_tool_policy
check "T1 worktree mode: seatbelt wrapper" "${WRAP[*]}" "sandbox-exec -f $PROF"
check "T2 worktree mode: tool args unchanged" "${TOOL_ARGS[*]}" "--permission-mode bypassPermissions --disallowedTools Task,Agent"
( WRITE_PROFILE=""; select_tool_policy 2>/dev/null ); check "T3 worker refuses write mode without a rendered profile" "$?" "1"
( PATH=/nonexistent; write_sandbox_available 2>/dev/null ); check "T4 sandbox-exec absent -> write gate refuses" "$?" "1"
( PATH=/nonexistent; select_tool_policy 2>/dev/null ); check "T5 worker refuses write mode without sandbox-exec" "$?" "1"

# --- work location injected into the worker prompt (DGN-1504 (c)) -----------
BS="$(worker_backstop)"
has "B1 backstop names the own worktree as the only writable checkout" "$BS" "your ONLY writable checkout is $WT_PATH"
has "B2 backstop maps the shared checkout onto the worktree" "$BS" "When the task names $CANON"
has "B3 backstop names the branch" "$BS" "(branch dsp/t1, cut from dev"
BS_RO="$( WT_PATH=""; worker_backstop )"
lacks "B4 observe backstop unchanged (no work-location block)" "$BS_RO" "WORK LOCATION"

# --- evidence rail --------------------------------------------------------------
checkout_snapshot "$RUNDIR/s0.tsv"
check "E1 snapshot: one line per shared checkout (canon + workspace)" "$(wc -l < "$RUNDIR/s0.tsv" | tr -d ' ')" "2"
check "E2 spawn note silent when the shared checkout is clean on base" "$(checkout_spawn_note "$RUNDIR/s0.tsv")" ""
checkout_snapshot "$RUNDIR/s1.tsv"
check "E3 no drift when nothing moved" "$(checkout_drift "$RUNDIR/s0.tsv" "$RUNDIR/s1.tsv")" ""
echo edit >> "$CANON/README"
echo new > "$CANON/untracked.txt"
checkout_snapshot "$RUNDIR/s2.tsv"
D="$(checkout_drift "$RUNDIR/s0.tsv" "$RUNDIR/s2.tsv")"
has "E4 drift: sibling's uncommitted edit is recorded (the invisible case)" "$D" "$CANON: uncommitted 0->2"
git -C "$CANON" add -A && git -C "$CANON" commit -qm sibling
checkout_snapshot "$RUNDIR/s3.tsv"
D="$(checkout_drift "$RUNDIR/s0.tsv" "$RUNDIR/s3.tsv")"
has "E5 drift: commit onto dev in the shared checkout is recorded" "$D" " dev "
has "E5b drift: HEAD move recorded" "$D" "HEAD "
git -C "$CANON" checkout -q -b someone-else
echo x > "$CANON/wip"
checkout_snapshot "$RUNDIR/s4.tsv"
N="$(checkout_spawn_note "$RUNDIR/s4.tsv")"
has "E6 spawn note: shared checkout off base is named" "$N" "is on someone-else (base dev) with 1 uncommitted"
has "E6b spawn note: says the run is unaffected" "$N" "cut from refs/heads/dev"
git -C "$CANON" checkout -q dev 2>/dev/null; rm -f "$CANON/wip"
checkout_snapshot "$RUNDIR/s5.tsv"
mkdir -p "$WSR/.telegram_bot/dispatch/dsp-x" "$WSR/product"
echo row > "$WSR/.telegram_bot/dispatch/dsp-x/status.json"
echo row > "$WSR/product/auto-loop-ledger.md"
checkout_snapshot "$RUNDIR/s6.tsv"
check "E7 dispatcher bookkeeping (run dirs, ledger) is not drift" "$(checkout_drift "$RUNDIR/s5.tsv" "$RUNDIR/s6.tsv")" ""

# --- real kernel (macOS only) ---------------------------------------------------
if [[ -n "$REAL_SANDBOX" && "$(uname)" == Darwin ]]; then
  ks() { "$REAL_SANDBOX" -f "$PROF" /bin/sh -c "$1" >/dev/null 2>&1; }
  ks "touch '$CANON/kx'";             check "K1 kernel: write into canonical checkout denied" "$([[ -e $CANON/kx ]] && echo written || echo denied)" "denied"
  ks "touch '$OWN/kx'";               check "K2 kernel: write into own worktree allowed" "$([[ -e $OWN/kx ]] && echo written || echo denied)" "written"
  ks "cd '$OWN' && git add kx && git commit -qm k"; check "K3 kernel: commit in own worktree works" "$(git -C "$OWN" log -1 --format=%s)" "k"
  BEFORE="$(git -C "$CANON" rev-parse dev)"
  ks "cd '$OWN' && git update-ref refs/heads/dev HEAD"; check "K4 kernel: moving dev denied" "$(git -C "$CANON" rev-parse dev)" "$BEFORE"
  ks "git -C '$CANON' checkout -q -b kbranch"; check "K5 kernel: switching the shared checkout denied" "$(git -C "$CANON" symbolic-ref --short HEAD)" "dev"
  ks "cd '$OWN' && git worktree add -q '$EST/.wt/k2' -b k2"; check "K6 kernel: git worktree add beside the estate works" "$([[ -d $EST/.wt/k2 ]] && echo yes || echo no)" "yes"
else
  SKIP=$((SKIP+1)); echo "SKIP K1-K6 real-kernel block (no usable macOS sandbox-exec: absent, or nested inside a sandbox)"
fi

# --- registered shared checkouts (live verify 2026-09-27 FAIL) ------------------
# An instance workspace that is NOT a sibling of canonical (the instance
# lives under <estate>/agents/, canonical elsewhere) dispatches --worktree on itself.
# Adjacency (estate_roots) never reaches canonical; the instance's
# .instance.conf DOGANY_REPO_ROOT must.
CREW="$LAB/crew/agents"; mkdir -p "$CREW"
METAL="$CREW/metal"; mk_repo "$METAL" metal
mk_repo "$CREW/skull" skull
WORKSPACE="$METAL"; WT_REPO="$METAL"; WT_SLUG="m1"; WT_BASE_ARG=""; RUNID="dsp-test-0002"
provision_worktree; check "C0 provision_worktree in the instance workspace" "$?" "0"
MOWN="$(cd "$WT_PATH" && pwd -P)"
# Negative control: no .instance.conf -> canonical is out of reach of the
# adjacency scan. This is the pre-fix state for every source below.
write_roster; ROSTER="$(printf '%s\n' "${ROSTER_CHECKOUTS[@]}")"
lacks "C1 control: without registration canonical is NOT on the roster (adjacency gap)" "$ROSTER" "$CANON"
has   "C1b control: adjacency still closes the sibling instance"   "$ROSTER" "$CREW/skull"
printf 'DOGANY_AGENT_NAME=metal\nDOGANY_REPO_ROOT=%s\n' "$CANON" > "$METAL/.instance.conf"
write_roster; ROSTER="$(printf '%s\n' "${ROSTER_CHECKOUTS[@]}")"
has   "C2 .instance.conf DOGANY_REPO_ROOT: canonical main checkout on the roster" "$ROSTER" "$CANON"
has   "C2b ... and canonical's linked worktrees"                  "$ROSTER" "$EST/.wt/sib"
lacks "C2c own worktree still excluded"                          "$ROSTER" "$MOWN"
MPROF="$LAB/run/write-metal.sb"
render_write_profile "$MPROF"; check "C3 render" "$?" "0"
mv() { python3 "$SBMODEL" verdict "$MPROF" 'file-write*' "$1"; }
check "C4 canonical VERSION (the live-verify probe) -> deny" "$(mv "$CANON/VERSION")" "deny"
check "C4b canonical dev ref -> deny"                        "$(mv "$GD/refs/heads/dev")" "deny"
check "C4c canonical HEAD (branch switch) -> deny"           "$(mv "$GD/HEAD")" "deny"
check "C5 own worktree -> allow"                             "$(mv "$MOWN/x")" "allow"
check "C5b instance workspace checkout -> deny"              "$(mv "$METAL/README")" "deny"
write_profile_selfcheck "$MPROF" >/dev/null 2>&1; check "C6 self-check passes (and probes canonical)" "$?" "0"
( DOGANY_ESTATE_ROOTS="$LAB/nowhere"; write_roster; printf '%s\n' "${ROSTER_CHECKOUTS[@]}" ) > "$LAB/c7"
has   "C7 DOGANY_ESTATE_ROOTS narrows adjacency, never the registered list" "$(cat "$LAB/c7")" "$CANON"
rm -f "$METAL/.instance.conf"
mkdir -p "$METAL/config"; printf 'SHARED_CHECKOUTS="%s:%s"\n' "$LAB/absent" "$OTHER" > "$METAL/config/agent.conf"
write_roster; ROSTER="$(printf '%s\n' "${ROSTER_CHECKOUTS[@]}")"
has   "C8 config/agent.conf SHARED_CHECKOUTS registers a checkout" "$ROSTER" "$OTHER"
lacks "C8b a registered path that does not exist is skipped"      "$ROSTER" "$LAB/absent"
rm -f "$METAL/config/agent.conf"
( DOGANY_SHARED_CHECKOUTS="$CANON"; write_roster; printf '%s\n' "${ROSTER_CHECKOUTS[@]}" ) > "$LAB/c9"
has   "C9 DOGANY_SHARED_CHECKOUTS env registers a checkout" "$(cat "$LAB/c9")" "$CANON"
# Canonical IS the target and also registered: only this run's worktree opens.
( WORKSPACE="$METAL"; WT_REPO="$CANON"; WT_PATH="$OWN"; DOGANY_SHARED_CHECKOUTS="$CANON"
  write_roster; render_write_profile "$LAB/run/write-canon.sb" >/dev/null 2>&1
  printf '%s %s %s\n' "$(python3 "$SBMODEL" verdict "$LAB/run/write-canon.sb" 'file-write*' "$OWN/x")" \
    "$(python3 "$SBMODEL" verdict "$LAB/run/write-canon.sb" 'file-write*' "$CANON/VERSION")" \
    "$(python3 "$SBMODEL" verdict "$LAB/run/write-canon.sb" 'file-write*' "$MOWN/x")" ) > "$LAB/c10"
check "C10 canonical is the target: own worktree allow, canonical checkout deny, other worktree deny" "$(cat "$LAB/c10")" "allow deny deny"
if [[ -n "$REAL_SANDBOX" && "$(uname)" == Darwin ]]; then
  printf 'DOGANY_REPO_ROOT=%s\n' "$CANON" > "$METAL/.instance.conf"
  write_roster; render_write_profile "$MPROF"
  "$REAL_SANDBOX" -f "$MPROF" /usr/bin/python3 -c "open('$CANON/VERSION','a')" >/dev/null 2>&1
  check "K7 kernel: append-open canonical VERSION from an instance-workspace run denied" "$([[ -e $CANON/VERSION ]] && echo opened || echo denied)" "denied"
  "$REAL_SANDBOX" -f "$MPROF" /bin/sh -c "touch '$MOWN/kx'" >/dev/null 2>&1
  check "K8 kernel: own worktree still writable" "$([[ -e $MOWN/kx ]] && echo written || echo denied)" "written"
else
  SKIP=$((SKIP+1)); echo "SKIP K7-K8 real-kernel block (no usable macOS sandbox-exec: absent, or nested inside a sandbox)"
fi

printf '\n%d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
[[ $FAIL -eq 0 ]]
