#!/usr/bin/env bash
# test-lane: slow    # spawns fake workers and waits for finalize
# test-dispatch-display-name.sh -- the dispatch display-name field + gate.
#
# One dispatch carries TWO names: the MACHINE label (--label: ledger/run/log
# key, ASCII hyphenated) and the name a HUMAN reads (--display-name). Before
# the split, owner surfaces rendered the machine label verbatim -- an English
# hyphenated job title landed on the owner's own work board on an instance
# that does not speak English.
#
# The locks this suite exists for:
#   (a) a dispatch with no display name IS REFUSED on a non-English instance
#       (not warned past): an optional field is the one skipped under load,
#       and the owner surface then falls back to the machine label silently;
#   (b) the language knob that decides is AGENT_LANG in config/agent.conf,
#       read through the shell rail's own reader lib/agentlib.sh -- proven by
#       flipping ONLY that file between the ko and en fixtures;
#   (c) the machine label keeps every machine use (branch, run dir, ledger
#       key, run.env/status.json label) untouched;
#   (d) the owner-facing render is the display name VERBATIM -- this change
#       authors no owner-facing wording of its own;
#   (e) the pending-copy marker on the owner completion-push copy stays
#       attached (that copy is an open owner-confirmation gate).
#
# Run: bash routines/tests/test-dispatch-display-name.sh
# Exit: 0 all pass, nonzero any fail.
#
# Safe/offline: fake claude stub via PATH, push/usage-gate stubs, scratch
# workspace under mktemp. No real spawn, no telegram, no live ledger.

set -uo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HARNESS_DIR/../.." && pwd)"
WRAPPER="$REPO/routines/dispatch-detached.sh"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TDIR="$(mktemp -d /tmp/dispname-test.XXXXXX)"
WS="$TDIR/ws"
cleanup() {
  pkill -f "$TDIR" 2>/dev/null
  rm -rf "$TDIR"
}
trap cleanup EXIT

mkdir -p "$WS/routines/lib" "$WS/config" "$WS/product" "$WS/.claude" \
         "$WS/.telegram_bot" "$TDIR/bin"
cp "$WRAPPER" "$WS/routines/"
cp "$REPO/routines/lib/ledger-update.sh" "$WS/routines/lib/"
cp "$REPO/routines/lib/agentlib.sh" "$WS/routines/lib/"
cp "$REPO/routines/lib/dispatch-tier-bindings.tsv" "$WS/routines/lib/"
cp "$REPO/routines/terminal-state-ledger.py" "$WS/routines/" 2>/dev/null || true
cat > "$WS/routines/usage-gate.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$WS/routines/push.sh" <<'EOF'
#!/bin/bash
exit 0
EOF
cat > "$TDIR/bin/claude" <<'EOF'
#!/bin/bash
sleep "${FAKE_CLAUDE_SLEEP:-1}"
echo "FAKE-CLAUDE-DONE"
EOF
chmod +x "$WS/routines/dispatch-detached.sh" "$WS/routines/usage-gate.sh" \
  "$WS/routines/push.sh" "$WS/routines/lib/ledger-update.sh" "$TDIR/bin/claude"

# The ONE language knob under test. set_lang rewrites only this file.
set_lang() { printf 'AGENT_LANG=%s\n' "$1" > "$WS/config/agent.conf"; }

fresh_ledger() {
  cat > "$WS/product/auto-loop-ledger.md" <<'EOF'
# ledger (test scratch)

| branch | item | state | attempts | backoff_until | last_ts | flags | note |
|---|---|---|---|---|---|---|---|
EOF
}
fresh_ledger

git -C "$WS" init -q -b main >/dev/null
git -C "$WS" config user.email t@t
git -C "$WS" config user.name t
git -C "$WS" add -A >/dev/null
git -C "$WS" commit -qm seed >/dev/null

export PATH="$TDIR/bin:$PATH"
export DOGANY_DISPATCH_WT_BASE="$TDIR/wt"
DD="$WS/routines/dispatch-detached.sh"
printf 'display-name probe task' > "$TDIR/task.md"

# The owner-facing name used throughout: human-readable, in the instance's
# language, with a space -- everything a machine label may not be. Non-ASCII
# on purpose: the field exists precisely because owner-facing names are not
# English, so the fixture must prove the value survives byte-exact through
# printf %q, the sourced run.env, the ledger row and status.json. This is
# caller DATA, not wording authored by this change (T3B asserts byte equality).
DISPLAY="관계도 모바일 렌더 정리"
LABELED="kmsp-map-mobile-render"

wait_finalized() {
  local i
  for i in $(seq 1 "$2"); do
    [[ -d "$1/.finalized" ]] && return 0
    sleep 1
  done
  return 1
}
runid_of() { sed -n 's/.*runid=\(dsp-[0-9-]*\).*/\1/p' <<< "$1"; }
# item column of the ledger row for a given branch (| branch | item | ...).
ledger_item() {
  grep -F "| $1 |" "$WS/product/auto-loop-ledger.md" | head -1 \
    | cut -d'|' -f3 | sed 's/^ *//;s/ *$//'
}

echo "== T1. non-English instance + no display name -> REFUSED (the core lock) =="
set_lang ko
"$DD" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --label "$LABELED" >/dev/null 2>"$TDIR/t1.err"
RC=$?
[[ $RC -eq 1 ]] && ok "T1A missing --display-name -> refused (exit 1, usage-error shape)" \
  || bad "T1A missing --display-name -> refused (got exit $RC)"
grep -q -- "--display-name is required" "$TDIR/t1.err" \
  && ok "T1B refusal names the missing field" || bad "T1B refusal names the missing field"
grep -q "language is 'ko'" "$TDIR/t1.err" \
  && ok "T1C refusal states WHY it applies here (resolved language)" \
  || bad "T1C refusal states the resolved language"
grep -q -- "--label ${LABELED} --display-name" "$TDIR/t1.err" \
  && ok "T1D refusal shows what to supply, with the caller's own label" \
  || bad "T1D refusal shows what to supply"
[[ ! -d "$WS/.telegram_bot/dispatch" ]] || [[ -z "$(ls "$WS/.telegram_bot/dispatch" 2>/dev/null)" ]] \
  && ok "T1E refused run left no rundir" || bad "T1E refused run left no rundir"
COUNT="$(cat "$WS/.telegram_bot/dispatch/active-count" 2>/dev/null || echo 0)"
[[ "${COUNT:-0}" == "0" ]] && ok "T1F refused run holds no active-count slot" \
  || bad "T1F refused run holds no active-count slot (got $COUNT)"
grep -q "$LABELED" "$WS/product/auto-loop-ledger.md" \
  && bad "T1G refused run wrote no ledger row" \
  || ok "T1G refused run wrote no ledger row"

echo "== T2. blank / separator-carrying display names are refused too =="
"$DD" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --label "$LABELED" --display-name "   " >/dev/null 2>"$TDIR/t2.err"
[[ $? -eq 1 ]] && grep -q -- "--display-name is blank" "$TDIR/t2.err" \
  && ok "T2A whitespace-only display name refused (would render as nothing)" \
  || bad "T2A whitespace-only display name refused"
"$DD" --task "$TDIR/task.md" --model sonnet --bottleneck accuracy \
  --label "$LABELED" --display-name "bad | name" >/dev/null 2>"$TDIR/t2b.err"
[[ $? -eq 1 ]] && grep -q "invalid --display-name" "$TDIR/t2b.err" \
  && ok "T2B pipe in display name refused (the ledger row is pipe-delimited)" \
  || bad "T2B pipe in display name refused"

echo "== T3. ko instance + display name: owner surface renders the NAME =="
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task.md" --model sonnet \
  --bottleneck accuracy --label "$LABELED" --display-name "$DISPLAY" \
  --ticket DGN-1 --timeout 2 --notify silent)"
RC=$?
RUNID="$(runid_of "$OUT")"
RUNDIR="$WS/.telegram_bot/dispatch/$RUNID"
[[ $RC -eq 0 && -n "$RUNID" ]] && ok "T3A label + display name -> spawned" \
  || bad "T3A label + display name -> spawned (rc=$RC)"
ITEM="$(ledger_item "dsp/$RUNID")"
[[ "$ITEM" == "$DISPLAY [DGN-1]" ]] \
  && ok "T3B work-board item column = display name VERBATIM (+ ticket), no authored wording" \
  || bad "T3B board item is the display name verbatim (got '$ITEM')"
grep -q "$LABELED" <<< "$ITEM" \
  && bad "T3C machine label absent from the owner-facing item" \
  || ok "T3C machine label absent from the owner-facing item"
grep -q "owner-facing name: $DISPLAY \[DGN-1\] (lang=ko)" <<< "$OUT" \
  && ok "T3D selection is announced on the spawn line, never silent" \
  || bad "T3D selection is announced on the spawn line"
( source "$RUNDIR/run.env" 2>/dev/null && [[ "$DISPLAY_NAME" == "$DISPLAY" ]] ) \
  && ok "T3E run.env records DISPLAY_NAME verbatim" || bad "T3E run.env records DISPLAY_NAME"
( source "$RUNDIR/run.env" 2>/dev/null && [[ "$LABEL" == "$LABELED" ]] ) \
  && ok "T3F run.env LABEL is still the machine label (field is additive)" \
  || bad "T3F run.env LABEL unchanged"
echo "== T4. machine label keeps its machine uses =="
[[ "$RUNID" == dsp-* && "$RUNID" != *"$LABELED"* && "$RUNID" != *"$DISPLAY"* ]] \
  && ok "T4A run dir id derives from neither name (timestamp+nonce, unchanged)" \
  || bad "T4A run dir id derives from neither name (got $RUNID)"
( source "$RUNDIR/run.env" 2>/dev/null && [[ "$LEDGER_BRANCH" == "dsp/$RUNID" ]] ) \
  && ok "T4B ledger key is dsp/<runid>, untouched by either name" \
  || bad "T4B ledger key is dsp/<runid>"
if wait_finalized "$RUNDIR" 40; then
  python3 - "$RUNDIR/status.json" "$DISPLAY" "$LABELED" <<'PY' \
    && ok "T4C status.json carries display_name AND the unchanged label" \
    || bad "T4C status.json carries display_name and label"
import json, sys
d = json.load(open(sys.argv[1]))
assert d["display_name"] == sys.argv[2], d
assert d["label"] == sys.argv[3], d
PY
else
  bad "T4C status.json carries display_name (run never finalized)"
fi

echo "== T5. worktree mode: the BRANCH is cut from the machine slug only =="
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task.md" --model sonnet \
  --bottleneck accuracy --label "$LABELED" --display-name "$DISPLAY" \
  --worktree wt-probe --timeout 2 --notify silent)"
RC=$?
RUNID2="$(runid_of "$OUT")"
[[ $RC -eq 0 ]] && ok "T5A worktree dispatch spawns with both names" \
  || bad "T5A worktree dispatch spawns (rc=$RC)"
if git -C "$WS" show-ref --verify --quiet refs/heads/dsp/wt-probe; then
  ok "T5B branch is dsp/<worktree-slug> -- neither name reached the branch"
else
  bad "T5B branch is dsp/<worktree-slug>"
fi
# Capture first, match second. Piping into `grep -q` under `set -o pipefail`
# kills the producer with SIGPIPE the moment grep has seen enough, and the
# pipeline then reports 141 whatever grep decided -- here that would turn a
# real hit (display name leaked into a ref) into a silent pass.
REF_NAMES="$(git -C "$WS" for-each-ref --format='%(refname)' refs/heads)"
if grep -qF "$DISPLAY" <<< "$REF_NAMES"; then
  bad "T5C no display name in any ref name"
else
  ok "T5C no display name in any ref name"
fi
wait_finalized "$WS/.telegram_bot/dispatch/$RUNID2" 40 || bad "T5 finalize"

echo "== T6. English instance: AGENT_LANG is the knob, and the label renders =="
set_lang en
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task.md" --model sonnet \
  --bottleneck accuracy --label "$LABELED" --timeout 2 --notify silent)"
RC=$?
RUNID3="$(runid_of "$OUT")"
[[ $RC -eq 0 && -n "$RUNID3" ]] \
  && ok "T6A no display name is NOT a refusal on an English instance (same argv that T1 refused)" \
  || bad "T6A English instance spawns without a display name (rc=$RC)"
ITEM="$(ledger_item "dsp/$RUNID3")"
[[ "$ITEM" == "$LABELED" ]] \
  && ok "T6B English instance renders the machine label (selection, not presence)" \
  || bad "T6B English instance renders the machine label (got '$ITEM')"
wait_finalized "$WS/.telegram_bot/dispatch/$RUNID3" 40 || bad "T6 finalize"

OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task.md" --model sonnet \
  --bottleneck accuracy --label "$LABELED" --display-name "$DISPLAY" \
  --timeout 2 --notify silent)"
RUNID4="$(runid_of "$OUT")"
ITEM="$(ledger_item "dsp/$RUNID4")"
[[ "$ITEM" == "$LABELED" ]] \
  && ok "T6C English instance ignores a supplied display name for the render" \
  || bad "T6C English instance ignores a supplied display name (got '$ITEM')"
( source "$WS/.telegram_bot/dispatch/$RUNID4/run.env" 2>/dev/null \
  && [[ "$DISPLAY_NAME" == "$DISPLAY" ]] ) \
  && ok "T6D ... but still records it (selection is at render time only)" \
  || bad "T6D English instance records the display name"
wait_finalized "$WS/.telegram_bot/dispatch/$RUNID4" 40 || bad "T6 finalize 2"

echo "== T7. missing language config -> default, and the gate is not half-armed =="
rm -f "$WS/config/agent.conf"
OUT="$(FAKE_CLAUDE_SLEEP=1 "$DD" --task "$TDIR/task.md" --model sonnet \
  --bottleneck accuracy --label "$LABELED" --timeout 2 --notify silent)"
RC=$?
RUNID5="$(runid_of "$OUT")"
[[ $RC -eq 0 ]] && grep -q "(lang=en)" <<< "$OUT" \
  && ok "T7A absent conf -> the established en default (agentlib contract), announced" \
  || bad "T7A absent conf -> en default (rc=$RC)"
ITEM="$(ledger_item "dsp/$RUNID5")"
[[ "$ITEM" == "$LABELED" ]] \
  && ok "T7B the render falls back with the gate -- never 'no name demanded, no name shown'" \
  || bad "T7B render and gate fall back together (got '$ITEM')"
wait_finalized "$WS/.telegram_bot/dispatch/$RUNID5" 40 || bad "T7 finalize"
set_lang ko

echo "== T8. backward compat: a run.env with no DISPLAY_NAME line (set -u) =="
OLDDIR="$WS/.telegram_bot/dispatch/dsp-20260101-000000-0001"
mkdir -p "$OLDDIR"
printf 'RUNID=dsp-20260101-000000-0001\nLABEL=legacy-run\nMODEL=fable\n' > "$OLDDIR/run.env"
# A pid file that points at an exited process. Present deliberately: do_list
# reads pid unconditionally for a rundir with no status.json, and a rundir
# missing BOTH files trips an unrelated pre-existing failure in that reader
# (reproduced on the unmodified baseline) -- which would hide the one thing
# this case is here to prove: a run.env with no DISPLAY_NAME line is set -u
# safe.
bash -c 'echo $$' > "$OLDDIR/pid"
"$DD" --list > "$TDIR/t8.out" 2>&1 \
  && ok "T8A --list survives a pre-field run.env" || bad "T8A --list survives a pre-field run.env"
grep -q "legacy-run" "$TDIR/t8.out" \
  && ok "T8B pre-field run still renders its machine label" \
  || bad "T8B pre-field run still renders"

echo "== T9. owner-facing copy discipline =="
# The completion push copy was an open owner-confirmation gate (dec-094)
# wholesale. As of 2026-09-11 it is SPLIT: the owner authored and confirmed
# the completed and failed branches verbatim and left timeout/cancel draft.
# The pin follows the split -- confirmed copy must carry a dated approval
# record and NOT the pending marker (else the next pass re-words settled
# copy), while the draft branches must still carry the marker (else draft copy
# graduates by nobody noticing). Matchers stay spelling-tolerant.
PUSH_BLOCK="$(awk '/# Push: issued by THIS shell/,/PUSH_SH/' "$WRAPPER")"
grep -qE 'approved.*2026-09-11|2026-09-11.*approved' <<< "$PUSH_BLOCK" \
  && ok "T9A the confirmed push copy carries a dated owner-approval record" \
  || bad "T9A the confirmed push copy carries a dated owner-approval record"
if grep -q 'PENDING OWNER CONFIRMATION' <<< "$PUSH_BLOCK"; then
  bad "T9A2 no pending marker sits on the confirmed push copy"
else
  ok "T9A2 no pending marker sits on the confirmed push copy"
fi
grep -q "dec-094" <<< "$PUSH_BLOCK" \
  && ok "T9B the copy gate is still named (dec-094)" || bad "T9B the copy gate is still named"
# The unconfirmed result branches moved into their own helper so the marker
# window can tell them apart from the approved pair. The marker must still be
# on them, and the push block must still route through the helper.
awk '/^draft_result_text\(\)/,/^}/' "$WRAPPER" > "$TDIR/t9.draft"
[[ -s "$TDIR/t9.draft" ]] \
  && ok "T9D the unconfirmed result branches live in their own helper" \
  || bad "T9D the unconfirmed result branches live in their own helper"
# Same capture-then-match rule as T5C: this window runs to the end of the
# file, so `grep -q` exits on its first hit long before awk is done and
# pipefail turns the SIGPIPE into 141 -- the marker was attached the whole
# time and the assertion still reported red.
DRAFT_WINDOW="$(awk '/# DRAFT owner-facing copy/,/^draft_result_text\(\)/' "$WRAPPER")"
if grep -qE 'PLACEHOLDER|DRAFT|PENDING OWNER CONFIRMATION|[Dd]o not treat this wording as final' <<< "$DRAFT_WINDOW"; then
  ok "T9D2 pending-copy marker still attached to the DRAFT result copy"
else
  bad "T9D2 pending-copy marker still attached to the DRAFT result copy"
fi
grep -q "draft_result_text" <<< "$PUSH_BLOCK" \
  && ok "T9D3 the push block routes the unconfirmed branches through it" \
  || bad "T9D3 the push block routes the unconfirmed branches through it"
# The push must actually RENDER the owner-facing name (owner ruling
# 2026-09-11, reversing the nameless notice) and must resolve the locale
# first: the worker sources run.env, which carries no locale, so without it
# the selector sits on its English default and the machine label reaches a
# non-English owner surface.
grep -q 'owner_facing_name' <<< "$PUSH_BLOCK" \
  && ok "T9E the completion push renders the owner-facing name" \
  || bad "T9E the completion push renders the owner-facing name"
grep -q 'resolve_owner_lang' <<< "$PUSH_BLOCK" \
  && ok "T9E2 the push resolves the locale before selecting the name" \
  || bad "T9E2 the push resolves the locale before selecting the name"
# Nothing this change renders to the owner is authored text: the board item is
# exactly the caller's display name plus the pre-existing ticket suffix (T3B
# asserts byte equality). The selection helper must therefore hold no literal.
awk '/^owner_facing_name\(\)/,/^}/' "$WRAPPER" > "$TDIR/t9.fn"
grep -qE '"[^"$]*[a-z]{4,}[^"$]*"' "$TDIR/t9.fn" \
  && bad "T9C name selection contains no owner-facing literal: $(cat "$TDIR/t9.fn")" \
  || ok "T9C name selection contains no owner-facing literal (pure selection)"

echo
echo "RESULT: PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]] || exit 1
exit 0
