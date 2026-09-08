#!/usr/bin/env bash
# routines/dispatch-detached.sh -- session-detached background dispatch (DGN-991 rev3, phase 1)
#
# WHY: interactive Task subagents live IN-PROCESS inside the session CLI.
# /stop (soft interrupt or hard teardown SIGKILL) kills them with the turn.
# This wrapper spawns long autonomous work as a claude -p worker in its OWN
# SESSION (python3 fork -> os.setsid -> exec, same escape as
# bridge/self_restart.sh DGN-1010), so /stop only cuts the conversational
# turn and background work survives. bridge/* is untouched by design: the
# hard-teardown invariant ("no ownerless processes") is preserved by giving
# the detached worker a NEW owner = ledger row + run dir.
#
# USAGE:
#   dispatch-detached.sh --task <prompt-file> --model <fable|opus|sonnet|haiku>
#     --bottleneck <coverage|accuracy|judgment>   (DGN-1218: the TASK's
#         bottleneck axis; the model is checked against the measured binding
#         in lib/dispatch-tier-bindings.tsv -- data, never gate logic)
#     [--why "<one line>"]   (mandatory ONLY when the chosen model is outside
#         the measured binding for that axis; recorded verbatim in the ledger)
#     --label <label> [--ticket DGN-NNN] [--worktree <slug>] [--timeout <min, default 60>]
#     [--notify silent|push]   (default: silent -- dec-126, owner sees results not events)
#     [--repo <path>]   (read-only mode only: worker cwd -> <path>; excludes --worktree)
#     [--worktree-repo <path>]   (requires --worktree: provision the worktree in THAT
#                                 git repo instead of the workspace -- WRITE-capable
#                                 target repo; artifacts stay workspace-anchored)
#     [--base <branch>]   (requires --worktree: branch point + freshness reference;
#                          default: dev when the target repo carries .dogany-canonical
#                          AND has a dev branch (DGN-1184 (A)), else main)
#   dispatch-detached.sh --cancel <runid>
#   dispatch-detached.sh --list
#   dispatch-detached.sh --calibration   (recent dispatch history -- the reader)
#   dispatch-detached.sh --backfill-model-observed [--apply]
#     (DGN-1227: retroactively write status.json.model_observed for past runs
#     whose stream.jsonl survived. Default dry-run (reports candidates,
#     writes nothing); --apply patches only the missing field, tagged
#     model_observed_backfilled=true. Never touches runs that already carry
#     the field or that have no stream.jsonl left.)
#
# CONTRACTS (locked spec: worklog/DGN-991-stop-kills-background-work.md rev3):
#   - Gates run SYNCHRONOUSLY before detach: usage-gate.sh (fail-CLOSED),
#     .claude/.fable-exhausted pre-check (fable -> opus demotion, logged),
#     active-count cap 3, prompt size cap 100KB (ARG_MAX).
#   - DGN-1158: the fable-exhausted flag is also WRITTEN from this script --
#     a fable worker that dies on the "requires usage credits" weekly-cap
#     signature records the flag in its own exit path (do_worker) so the
#     NEXT fable spawn's pre-check above demotes automatically. No auto
#     re-dispatch of the run that just died.
#   - BOTTLENECK GATE (DGN-1218): a spawn must declare the task's bottleneck
#     axis (--bottleneck coverage|accuracy|judgment). LAYER 1 (this script's
#     logic) knows only that axis enum -- it is a property of the TASK, not
#     of any model, so it survives every provider change. LAYER 2
#     (lib/dispatch-tier-bindings.tsv) is DATA binding (runtime, axis) ->
#     measured models (DGN-1193 replay measurement). A model outside the
#     binding -> spawn REFUSED unless --why states the exception (recorded
#     verbatim in run.env/status.json). A missing (runtime, axis) row ->
#     pass-through with a notice: an unmeasured runtime is never blocked
#     (3.0 boundary -- "do not build, do not block"). A missing data FILE ->
#     refused, fail-closed. Model names must NEVER appear in the gate logic;
#     a new runtime is new rows, zero code
#     (locked by tests/test-dgn1218-dispatch-gate.sh).
#   - CALIBRATION READER (DGN-1218): the spawn path prints the recent
#     dispatch history (model/axis/result/wall/product from status.json --
#     the ledger that already existed and nobody read) BEFORE the gates run,
#     so the tier decision is made against data, not memory. Standalone:
#     --calibration.
#   - active-count (.telegram_bot/dispatch/active-count) is a SEPARATE ledger
#     from autonomous-loop's DAILY_SPAWN_BUDGET / PER_RUN_CAP. The loop budget
#     governs queue juniors; this counter governs interactive detached
#     dispatches only. Never merge the two.
#   - --worktree <slug>: branch dsp/<slug> (namespace-separated from loop's
#     auto/*), freshness gate (the BASE branch must be ancestor). WITHOUT --worktree the
#     worker runs in OBSERVE mode: kernel-enforced read-only (macOS seatbelt,
#     profile rendered to $RUNDIR/observe.sb) with Bash fully open inside --
#     blocks a second writer on the live checkout. See "OBSERVE MODE" below.
#   - --worktree-repo <path> (requires --worktree, DGN-1012): the repo the
#     worktree is CUT FROM. Default = WORKSPACE, so a bare --worktree call is
#     byte-for-byte the old behaviour (zero regression). With it, a dispatch
#     can WRITE to another repo (canonical, a product repo) through the same
#     worktree isolation -- the combination that did not exist before and
#     forced four hand-carrybacks on 2026-08-24 (--worktree always landed in
#     the metal repo; --repo could reach another repo but only READ-ONLY).
#     Kept as its OWN flag rather than overloading --repo: --repo means
#     "read-only cwd redirect" and stays mutually exclusive with --worktree,
#     so one flag never means two things depending on its companions, and
#     write-to-foreign-repo dispatches are grep-able at the call site.
#     Everything except the git repo of the worktree is unchanged: RUNDIR /
#     out.log / status.json / session-inbox drop / ledger / active-count stay
#     WORKSPACE-anchored absolute paths. Freshness gate + cleanup run against
#     the TARGET repo (WT_REPO); the gate compares that repo's own BASE branch.
#   - BASE branch (DGN-1184 (A), 2026-09-01): the canonical framework repo
#     develops on `dev` (main = release line, dev -> main merges only). A
#     worktree cut from main there would build on a stale base and its merge
#     into dev would be judged against the wrong line. Resolution, printed on
#     the spawn line (never silent): --base <branch> if given; else `dev` when
#     the target repo root carries the tracked `.dogany-canonical` marker AND
#     refs/heads/dev exists (same marker scoping as git-hooks/pre-commit --
#     instance repos never carry it, so they keep main); else `main` (bare
#     workspace dispatch = byte-for-byte the old behaviour).
#   - --repo <path> (observe mode only, mutually exclusive with --worktree):
#     sets the worker cwd to <path>. cwd is the ONLY thing that moves:
#     RUNDIR / out.log / status.json / session-inbox drop / ledger /
#     active-count stay WORKSPACE-anchored absolute paths, and <path> joins
#     the sandbox deny list as the worker's WORKDIR. To WRITE in another repo
#     use --worktree <slug> --worktree-repo <path> instead.
#   - OBSERVE MODE (DGN-1191, replaces the --allowedTools whitelist): the
#     read/write boundary is a FILESYSTEM invariant, and the permission engine
#     only sees command-line text -- it cannot see that "git log --output=X",
#     "sqlite3 -readonly ... writefile()" or "python3 -m json.tool in out"
#     write files (all measured, docs/dgn1191-lab/). So the invariant is
#     enforced where it lives: the whole worker process tree runs under
#     sandbox-exec with (deny file-write*) and only /dev, TMPDIR, /tmp and
#     the CLI's own ~/.claude session state re-opened; WORKDIR / WORKSPACE /
#     WORKTREE_BASE are denied LAST (seatbelt: last matching rule wins, so
#     a target living under /tmp is still denied). Tool policy inside:
#     --permission-mode default --allowedTools Bash --disallowedTools
#     Task,Agent,Write,Edit,NotebookEdit. Write/Edit deny is belt only; the
#     kernel is the enforcement. sandbox-exec absent -> spawn REFUSED (rc 2),
#     never a silent fallback to an allowlist. Design + evidence:
#     docs/DGN-1191-observe-mode-design.md. Tests (model-free):
#     routines/tests/test-dispatch-observe.sh.
#   - Ledger writes go through routines/lib/ledger-update.sh ONLY. Running
#     note format mirrors the loop ("dispatch 스폰 (model=$MODEL)") so
#     status-footer.py's existing model= parser reads it unchanged.
#   - End states: commits present -> pending-review; report-only ->
#     deferred --flag dsp-report; timeout/error/cancel -> failed. No new
#     state enum.
#   - Push is issued by THIS shell (worker postprocess), never by the model.
#     Model output arrives as files only ("LLM judges, shell writes").
#   - No instance paths hardcoded: everything resolves from SCRIPT_DIR
#     (push.sh:35-39 pattern) -- canonical-bound.
#   - First-run bootstrap: product/auto-loop-ledger.md is seeded with an
#     EMPTY table skeleton when absent (fresh mints ship no product/ dir and
#     update.sh never syncs product/). Row writes still go through
#     lib/ledger-update.sh only -- the seed never contains a row.
#   - Completion return path (DGN-1012 closure axis): EVERY terminal state
#     (completed/timed-out/canceled/error) drops
#     <workspace>/.telegram_bot/session-inbox/dispatch-<runid>.md for the
#     bridge poller (bot.py _session_inbox_loop, DGN-217) to inject into the
#     Metal session -- the owner push alone leaves Metal blind to completion.
#     --notify silent suppresses the OWNER push only; the inbox drop always
#     happens. Owner push copy stays untouched (dec-094 UX gate pending).
#
# EXIT CODES (launcher): 0 spawned/ok, 1 usage/config error, 2 gate refused
#   (usage gate / active cap / observe sandbox unavailable or self-check
#   failed), 3 worktree/freshness failure.

set -euo pipefail

# ---------------------------------------------------------------------------
# Path resolution -- SCRIPT_DIR relative, no instance hardcoding (spec cond. 1)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="$(cd "$SCRIPT_DIR/.." && pwd)"
DISPATCH_DIR="$WORKSPACE/.telegram_bot/dispatch"
ACTIVE_COUNT_FILE="$DISPATCH_DIR/active-count"
COUNT_LOCK_DIR="$DISPATCH_DIR/.count.lock"
LEDGER_UPDATE="$SCRIPT_DIR/lib/ledger-update.sh"
USAGE_GATE="$SCRIPT_DIR/usage-gate.sh"
PUSH_SH="$SCRIPT_DIR/push.sh"
FABLE_FLAG="$WORKSPACE/.claude/.fable-exhausted"
LEDGER_FILE="$WORKSPACE/product/auto-loop-ledger.md"
# bot_data_dir of the bridge (config.bot_data_dir = <workspace>/.telegram_bot)
# -- same resolution as review-reminder.sh / decision-actions-watch.sh.
SESSION_INBOX_DIR="$WORKSPACE/.telegram_bot/session-inbox"
WORKTREE_BASE="${DOGANY_DISPATCH_WT_BASE:-/tmp/dogany-dispatch/worktrees}"
# DGN-1218 Layer 2: (runtime, axis) -> measured-model binding lives in DATA,
# never in this script's logic. New runtime = new rows there, zero code here.
TIER_BINDINGS="$SCRIPT_DIR/lib/dispatch-tier-bindings.tsv"
# Runtime/provider identifier recorded on every run (DGN-1218). This script
# only spawns the claude CLI today, so the default is a constant; the env
# override is the seam a future non-Claude spawn path (or a test) uses to
# label itself. Without this field in the ledger, cross-provider comparison
# is impossible later -- what is not measured does not exist (DGN-1193).
RUNTIME="${DOGANY_DISPATCH_RUNTIME:-claude-code}"

# 2026-08-26 13:0x: 3 -> 5 (형님 "태워") -> 13:2x: 1 (7일 창 97% critical)
#   -> 13:3x: 3 복귀 (형님 계정 전환, 새 창 7d 1% 실측).
# 교훈(주석으로 남긴다): 상한을 올리기 전에 잔량을 먼저 실측한다. 13:0x 인상 시점에
#   확인 안 했고, 40분 만에 7일 창이 97%까지 갔다. usage-gate 는 임계에서 거부할 뿐
#   임계까지 가는 속도는 이 상수가 정한다 -- "게이트가 있으니 괜찮다"는 틀린 추론.
#   기본값 3 = 설계값. 5 이상은 잔량 실측 + 형님 승인 동반일 때만.
#
# DGN-1262: 그 조건("실측 + 승인")이 충족돼도 올릴 방법이 상수 편집뿐이었다.
#   프레임워크 파일이라 라이브 인스턴스는 하드패치가 금지되고, 그래서 승인이
#   있어도 다음 업데이트까지 못 올렸다 -- 규율이 아니라 배선이 막고 있었다.
#   env override 를 연다: 기본값은 설계값 3 그대로이고, 상향은 호출마다 명시돼
#   호출 이력에 남는다(상수 편집은 아무 흔적도 안 남긴다).
#   올리기 전 잔량 실측 의무는 그대로다 -- 이 노브가 그 의무를 면제하지 않는다.
MAX_ACTIVE="${DOGANY_DISPATCH_MAX_ACTIVE:-3}"
case "$MAX_ACTIVE" in
  ''|*[!0-9]*|0) echo "[dispatch] DOGANY_DISPATCH_MAX_ACTIVE must be a positive integer (got '$MAX_ACTIVE')" >&2; exit 2 ;;
esac
PROMPT_MAX_BYTES=102400   # 100KB -- ARG_MAX guard (spec R8)
DEFAULT_TIMEOUT_MIN=60

# ---------------------------------------------------------------------------
# Argument parsing / mode routing
# ---------------------------------------------------------------------------
usage() {
  sed -n '13,37p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

MODE="spawn"
TASK_FILE=""
MODEL=""
LABEL=""
TICKET=""
# DGN-1218 gate inputs. Empty defaults double as the backward-compat floor
# when finalize/cancel/list source a run.env written before this gate
# existed (no BOTTLENECK/TIER_WHY lines; set -u would trip otherwise).
BOTTLENECK=""
TIER_WHY=""
WT_SLUG=""
REPO_PATH=""
WT_REPO_ARG=""
# Git repo the worktree is cut from. Default = workspace (legacy behaviour).
# Also the fallback when do_cancel sources a run.env written by an older
# version of this script (no WT_REPO line) -- set -u would trip otherwise.
WT_REPO="$WORKSPACE"
# Base branch of the dsp/<slug> worktree (branch point + freshness reference).
# Resolved in provision_worktree (see BASE branch contract in the header).
# Default "main" doubles as the fallback when finalize/cancel source a run.env
# written by an older version of this script (no WT_BASE line; set -u).
WT_BASE="main"
WT_BASE_ARG=""
# Observe-mode sandbox artifacts (empty in worktree mode and in run.env files
# written before DGN-1191 -- set -u would trip otherwise).
SANDBOX_PROFILE=""
SESSION_ID=""
TIMEOUT_MIN="$DEFAULT_TIMEOUT_MIN"
# dec-126 (owner, 2026-08-24): default = silent. The owner is NOT notified that
# a dispatch finished -- Metal receives the completion via the session-inbox
# drop (unconditional, see write_session_inbox) and reports the RESULT in its
# own words. What reaches the owner should be the outcome, not the event.
# --notify push stays available for the rare run the owner is actually waiting on.
NOTIFY="silent"
CANCEL_RUNID=""
WORKER_RUNDIR=""
BACKFILL_APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task)     TASK_FILE="$2"; shift 2 ;;
    --model)    MODEL="$2"; shift 2 ;;
    --bottleneck) BOTTLENECK="$2"; shift 2 ;;
    --why)      TIER_WHY="$2"; shift 2 ;;
    --label)    LABEL="$2"; shift 2 ;;
    --ticket)   TICKET="$2"; shift 2 ;;
    --worktree) WT_SLUG="$2"; shift 2 ;;
    --repo)     REPO_PATH="$2"; shift 2 ;;
    --worktree-repo) WT_REPO_ARG="$2"; shift 2 ;;
    --base)     WT_BASE_ARG="$2"; shift 2 ;;
    --timeout)  TIMEOUT_MIN="$2"; shift 2 ;;
    --notify)   NOTIFY="$2"; shift 2 ;;
    --cancel)   MODE="cancel"; CANCEL_RUNID="$2"; shift 2 ;;
    --list)     MODE="list"; shift 1 ;;
    --calibration) MODE="calibration"; shift 1 ;;
    --worker-run) MODE="worker"; WORKER_RUNDIR="$2"; shift 2 ;;
    --backfill-model-observed) MODE="backfill"; shift 1 ;;
    --apply)    BACKFILL_APPLY=1; shift 1 ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "[dispatch] unknown arg: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# active-count helpers -- SEPARATE ledger from loop budget (see header).
# mkdir-based lock: atomic on POSIX, self-expiring via stale-age check.
# ---------------------------------------------------------------------------
count_lock() {
  # First-run bootstrap: the lock mkdir fails forever if the parent dir is
  # absent (found by sandbox E2E -- spawn refused with a bogus 0/3 message).
  mkdir -p "$DISPATCH_DIR" 2>/dev/null || true
  local tries=0
  while ! mkdir "$COUNT_LOCK_DIR" 2>/dev/null; do
    tries=$(( tries + 1 ))
    if [[ $tries -ge 50 ]]; then
      # Stale lock (holder crashed): steal if older than 30s, else fail.
      local age now mtime
      now="$(date +%s)"
      mtime="$(stat -f %m "$COUNT_LOCK_DIR" 2>/dev/null || stat -c %Y "$COUNT_LOCK_DIR" 2>/dev/null || echo "$now")"
      age=$(( now - mtime ))
      if [[ $age -gt 30 ]]; then
        rmdir "$COUNT_LOCK_DIR" 2>/dev/null || true
        continue
      fi
      echo "[dispatch] active-count lock timeout" >&2
      return 1
    fi
    sleep 0.1
  done
  return 0
}

count_unlock() {
  rmdir "$COUNT_LOCK_DIR" 2>/dev/null || true
}

read_active_count() {
  local v="0"
  if [[ -f "$ACTIVE_COUNT_FILE" ]]; then
    v="$(cat "$ACTIVE_COUNT_FILE" 2>/dev/null || echo 0)"
    v="${v//[^0-9]/}"
  fi
  echo "${v:-0}"
}

# incr returns 1 (refuse) when the cap is already reached.
active_count_incr() {
  count_lock || return 1
  local v
  v="$(read_active_count)"
  if [[ $v -ge $MAX_ACTIVE ]]; then
    count_unlock
    return 1
  fi
  printf '%s\n' "$(( v + 1 ))" > "$ACTIVE_COUNT_FILE"
  count_unlock
  return 0
}

active_count_decr() {
  count_lock || return 0   # best-effort on the way down; never block teardown
  local v
  v="$(read_active_count)"
  if [[ $v -gt 0 ]]; then
    printf '%s\n' "$(( v - 1 ))" > "$ACTIVE_COUNT_FILE"
  else
    printf '0\n' > "$ACTIVE_COUNT_FILE"
  fi
  count_unlock
}

# ---------------------------------------------------------------------------
# Gate (b): .claude/.fable-exhausted pre-check -> opus demotion (spec R1).
# Flag semantics mirror usage-gate.py: JSON {"expiry": iso8601}; past-expiry
# flag is treated as recovered (we do NOT delete it -- that is usage-gate.py's
# surface; we only read). Unparseable expiry biases to demotion, capped by
# 7d file age (same conservatism as usage-gate.py).
# ---------------------------------------------------------------------------
fable_exhausted() {
  [[ -f "$FABLE_FLAG" ]] || return 1
  python3 - "$FABLE_FLAG" <<'PY'
import json, os, sys, time
from datetime import datetime, timezone
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        flag = json.load(f)
    expiry = flag.get("expiry", "") if isinstance(flag, dict) else ""
    exp = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    sys.exit(0 if exp > datetime.now(timezone.utc) else 1)
except Exception:
    # Corrupt flag: demote (safe) unless the file has aged past one 7d window.
    try:
        sys.exit(0 if (time.time() - os.path.getmtime(path)) <= 7 * 86400 else 1)
    except Exception:
        sys.exit(0)
PY
}

# ---------------------------------------------------------------------------
# DGN-1158: fable_exhausted() above only READS this flag; nothing in the
# estate ever wrote it, so the fable->opus demotion gate was a permanent
# no-op (observed: a fable worker died on "Fable 5 requires usage credits..."
# and the flag did not exist anywhere -- worklog/DGN-1158). This is the
# writer, called from the worker's own exit path (do_worker, below) on that
# exact death signature. It only consumes the failure as a SIGNAL for the
# next spawn's demotion -- it never retries the dispatch that just died; that
# decision stays with whoever reads the finalize/inbox report.
#
# DGN-1350: this pattern must stay in LOCKSTEP with RE_DEATH in the sibling
# writer routines/fable-death-catch.py. It did not, and the divergence was
# silent: the harness reworded the cap message to "You've reached your Fable 5
# limit. Switch to another model, or manage usage credits at ..." -- the
# python writer's `reached your .*limit` alternative still matched, this one
# ('requires usage credits') did not. Two detached fable dispatches died on
# that wording (2026-09-08 22:03 / 22:17), no marker was written either time,
# and the demotion gate stayed a no-op exactly as before DGN-1158 "fixed" it.
# Keep the alternation identical to the python side; the paired test asserts
# both writers accept the same corpus.
# ---------------------------------------------------------------------------
RE_FABLE_CREDITS_DEATH='[Rr]equires usage credits|[Hh]it your (usage )?limit|usage limit reached|weekly limit reached|[Rr]eached your .*limit'

# rc 0 = this run died on the fable weekly-cap signature, 1 = anything else.
is_fable_credits_death() {
  [[ "$MODEL" == "fable" ]] || return 1
  [[ -f "$1" ]] || return 1
  grep -qE "$RE_FABLE_CREDITS_DEATH" "$1"
}

# Same flag schema and same expiry source/fallback as the sibling PostToolUse
# writer (routines/fable-death-catch.py reset_expiry), so either writer
# leaves an interchangeable marker for the one reader (fable_exhausted() /
# usage-gate.py): the weekly cap resets on the 7d window boundary recorded in
# .claude/.usage-cache.json; missing/unparseable -> now+3d so the flag still
# self-clears and fable gets re-probed even without the cache.
write_fable_exhausted_marker() {
  mkdir -p "$(dirname "$FABLE_FLAG")" 2>/dev/null || return 1
  python3 - "$FABLE_FLAG" "$WORKSPACE" <<'PY'
import json, os, sys
from datetime import datetime, timedelta, timezone

flag_path, workspace = sys.argv[1], sys.argv[2]
cache = os.path.join(workspace, ".claude", ".usage-cache.json")
expiry = None
try:
    with open(cache, encoding="utf-8") as f:
        resets = json.load(f).get("seven_day_resets_at")
    if resets:
        datetime.fromisoformat(str(resets).replace("Z", "+00:00"))
        expiry = str(resets)
except Exception:
    expiry = None
if not expiry:
    expiry = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

tmp = flag_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({
        "expiry": expiry,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": "dispatch-detached-worker",
    }, f)
os.replace(tmp, flag_path)
PY
}

# ---------------------------------------------------------------------------
# First-run ledger bootstrap. ledger-update.sh refuses to write when the
# ledger file is absent (loud, by design) -- without this seed the very first
# dispatch on a freshly minted instance dies with "Ledger not found" (the
# caller-present/callee-absent defect class, DGN-1061). Seeds header + table
# head ONLY: an empty table keeps the one-write-path doctrine intact (rows
# are written exclusively by lib/ledger-update.sh), and the table head is
# required -- ledger-update.sh appends after the last pipe row and would
# silently DROP the row into a headless file. noclobber = first writer wins;
# a concurrent loser falls through to the existing file.
# ---------------------------------------------------------------------------
ensure_ledger() {
  if [[ -f "$LEDGER_FILE" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$LEDGER_FILE")"
  ( set -o noclobber
    printf '%s\n' \
      "# auto-loop-ledger.md" \
      "# Current-state ledger -- one row per background work item (branch)." \
      "# Single write path: routines/lib/ledger-update.sh. Never edit rows by hand." \
      "# Seeded by dispatch-detached.sh on first use (DGN-991 backland)." \
      "" \
      "| branch | item | state | attempts | backoff_until | last_ts | flags | note |" \
      "|---|---|---|---|---|---|---|---|" > "$LEDGER_FILE"
  ) 2>/dev/null || true
  if [[ ! -f "$LEDGER_FILE" ]]; then
    echo "[dispatch] ledger bootstrap failed: cannot create $LEDGER_FILE" >&2
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Spawn-side gates, all synchronous, all BEFORE detach (spec step 1).
# ---------------------------------------------------------------------------
run_gates() {
  # (a) usage gate -- fail-CLOSED: any non-zero exit refuses the spawn.
  local gate_out gate_rc=0
  gate_out="$("$USAGE_GATE" 2>&1)" || gate_rc=$?
  if [[ $gate_rc -ne 0 ]]; then
    echo "[dispatch] refused by usage-gate (rc=$gate_rc):" >&2
    echo "$gate_out" >&2
    return 2
  fi

  # (b) fable-exhausted pre-check -> opus demotion, explicitly logged.
  if [[ "$MODEL" == "fable" ]] && fable_exhausted; then
    echo "[dispatch] fable weekly cap exhausted (.claude/.fable-exhausted) -> demoting to opus for this dispatch"
    MODEL="opus"
  fi

  # (c) concurrent dispatch cap (separate ledger from loop budget).
  if ! active_count_incr; then
    echo "[dispatch] refused: active dispatch cap reached ($(read_active_count)/$MAX_ACTIVE). Cancel or wait, then retry." >&2
    return 2
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Prompt size cap -- claude -p receives the prompt as ONE argv entry; past
# ~100KB we risk E2BIG (ARG_MAX). Non-exec refusal with guidance (spec R8).
# ---------------------------------------------------------------------------
check_prompt_size() {
  local bytes
  bytes="$(wc -c < "$TASK_FILE" | tr -d ' ')"
  if [[ $bytes -gt $PROMPT_MAX_BYTES ]]; then
    echo "[dispatch] refused: prompt file is ${bytes}B > ${PROMPT_MAX_BYTES}B (ARG_MAX guard)." >&2
    echo "[dispatch] hint: move bulk material into files and reference their paths from a short prompt." >&2
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# DGN-1218 LAYER 2: (runtime, axis) -> measured-model binding, consulted as
# DATA. These functions must never contain a model name: the moment one
# appears here, a provider swap (3.0) has one more thing to rip out --
# guarded by tests/test-dgn1218-dispatch-gate.sh.
# ---------------------------------------------------------------------------

# tier_binding_lookup <runtime> <axis>
# stdout: comma-separated measured-model list for the row.
# rc 0 = row found, 1 = no row (unmeasured runtime/axis), 2 = data file absent.
tier_binding_lookup() {
  local runtime="$1" axis="$2"
  [[ -f "$TIER_BINDINGS" ]] || return 2
  awk -F'\t' -v rt="$runtime" -v ax="$axis" '
    /^[[:space:]]*(#|$)/ { next }
    $1 == rt && $2 == ax { print $3; found = 1; exit }
    END { exit found ? 0 : 1 }
  ' "$TIER_BINDINGS"
}

# Refuses (rc 2) when the declared axis and the chosen model disagree with the
# measured binding and no --why was given. The default path is REFUSAL, not a
# warning: warn-and-pass is exactly the 3.4% shape (DGN-1194). The override
# path exists because the binding is measurement over a finite sample, not
# doctrine -- but it must leave a verbatim, auditable record.
bottleneck_gate() {
  local models="" rc=0 m ok=0
  models="$(tier_binding_lookup "$RUNTIME" "$BOTTLENECK")" || rc=$?
  if [[ $rc -eq 2 ]]; then
    echo "[dispatch] REFUSED: tier-binding data file missing: $TIER_BINDINGS (fail-closed -- a lost data file must not silently disarm the gate)" >&2
    return 2
  fi
  if [[ $rc -eq 1 ]]; then
    # Unmeasured runtime (or axis row): never block -- measurement does not
    # exist yet and blocking would freeze new runtimes out (3.0 boundary).
    # The run is still fully recorded, so the future measurement can happen.
    echo "[dispatch] tier-binding: no row for (runtime=$RUNTIME, axis=$BOTTLENECK) -- unmeasured, passing through (run recorded for future measurement)"
    return 0
  fi
  for m in ${models//,/ }; do
    [[ "$m" == "$MODEL" ]] && ok=1
  done
  if [[ $ok -eq 1 ]]; then
    return 0
  fi
  if [[ -n "$TIER_WHY" ]]; then
    echo "[dispatch] tier-binding OVERRIDE: axis=$BOTTLENECK measured=[$models] chosen=$MODEL -- accepted with --why (recorded verbatim in run.env/status.json)"
    return 0
  fi
  {
    echo "[dispatch] REFUSED: axis/model mismatch (DGN-1218 tier-binding gate)."
    echo "[dispatch]   declared bottleneck : $BOTTLENECK"
    echo "[dispatch]   measured binding    : $models  (runtime=$RUNTIME, data: $TIER_BINDINGS)"
    echo "[dispatch]   chosen model        : $MODEL"
    echo "[dispatch] pick a bound model, or state the exception: --why \"<one line, >=10 chars, recorded>\"."
    echo "[dispatch] the binding is measurement (DGN-1193 replay), not preference -- overrides are ledgered and audited."
  } >&2
  return 2
}

# ---------------------------------------------------------------------------
# DGN-1218 calibration reader. The ledger (run.env + status.json) always
# recorded model/wall/result -- what was missing was a READER at the moment
# of decision. Printed on every spawn attempt BEFORE the gates, and on
# demand via --calibration. Deliberately cheap: newest <=20 status.json
# files, nothing else. Shows only decision-relevant columns: what tier ran,
# on which axis, how it ended, how long it took, what it produced.
# ---------------------------------------------------------------------------
print_calibration() {
  if [[ ! -d "$DISPATCH_DIR" ]]; then
    echo "[calibration] no dispatch history yet"
    return 0
  fi
  CAL_DIR="$DISPATCH_DIR" python3 - <<'PY' || echo "[calibration] reader failed (non-fatal)"
import datetime, json, os, sys

d = os.environ["CAL_DIR"]
try:
    names = sorted((n for n in os.listdir(d) if n.startswith("dsp-")), reverse=True)
except OSError:
    sys.exit(0)
rows = []
for n in names:
    if len(rows) >= 20:
        break
    try:
        with open(os.path.join(d, n, "status.json"), encoding="utf-8") as f:
            rows.append(json.load(f))
    except Exception:
        continue   # running or pre-status run: not calibration evidence yet
if not rows:
    print("[calibration] no finalized dispatch history yet")
    sys.exit(0)

def wall(doc):
    try:
        fmt = "%Y-%m-%dT%H:%M:%S%z"
        s = datetime.datetime.strptime(doc.get("started_at", ""), fmt)
        e = datetime.datetime.strptime(doc.get("ended_at", ""), fmt)
        return "%dm" % max(0, round((e - s).total_seconds() / 60))
    except Exception:
        return "?"

def product(doc):
    c = doc.get("commits") or 0
    if c:
        return "commits=%d" % c
    try:
        b = os.path.getsize(doc.get("out_log", ""))
    except OSError:
        b = 0
    return "report=%dKB" % round(b / 1024)

print("[calibration] recent dispatches (newest first):")
for doc in rows[:8]:
    print("  %s  %-6s %-9s %-10s %4s  %-12s %s%s" % (
        (doc.get("started_at") or "?")[5:16],
        doc.get("model") or "?",
        doc.get("bottleneck") or "-",
        doc.get("result") or "?",
        wall(doc),
        product(doc),
        doc.get("label") or "",
        (" [" + doc["ticket"] + "]") if doc.get("ticket") else ""))
hist, axed = {}, 0
for doc in rows:
    m = doc.get("model") or "?"
    hist[m] = hist.get(m, 0) + 1
    if doc.get("bottleneck"):
        axed += 1
dist = " / ".join("%s %d" % kv for kv in sorted(hist.items(), key=lambda kv: -kv[1]))
print("[calibration] last %d by model: %s -- bottleneck axis recorded on %d/%d"
      % (len(rows), dist, axed, len(rows)))
PY
  return 0
}

# ---------------------------------------------------------------------------
# Spawn path
# ---------------------------------------------------------------------------
# TARGET LAYER (DGN-1226 retroactive declaration, DGN-1194 wiring pass):
# the spawn-arg gates below (--model/--label refusal, usage gate) are
# Layer 2 -- a dev agent gating a dev agent; hard refusal (exit 1) is the
# correct failure shape at this layer.  The layer model is UNSETTLED
# (DGN-1226 sec 4) -- declaration only, no layer branching.  If devkit
# inherits this machinery, it must NOT reach Layer 3 (product users) in
# this form.
validate_spawn_args() {
  if [[ -z "$TASK_FILE" || -z "$MODEL" || -z "$LABEL" ]]; then
    echo "[dispatch] --task, --model, --label are required" >&2
    return 1
  fi
  if [[ ! -f "$TASK_FILE" || ! -r "$TASK_FILE" ]]; then
    echo "[dispatch] task file not readable: $TASK_FILE" >&2
    return 1
  fi
  case "$MODEL" in
    fable|opus|sonnet|haiku) ;;
    *) echo "[dispatch] invalid --model '$MODEL' (fable|opus|sonnet|haiku)" >&2; return 1 ;;
  esac
  # DGN-1218 LAYER 1: the bottleneck axis is REQUIRED, the same forcing point
  # as --model itself (a written rule runs at 3.4%; a refusing flag ran at
  # 100% -- DGN-1194). It asks "why this tier" in the one machine-checkable
  # form: which property of the TASK bounds the outcome. Axis names are task
  # properties, never model properties -- this block must stay model-name-free.
  if [[ -z "$BOTTLENECK" ]]; then
    {
      echo "[dispatch] --bottleneck is required: declare the task's bottleneck axis before choosing a tier (DGN-1218)."
      echo "[dispatch]   coverage  = exhaustive enumeration; one omission voids the artifact"
      echo "[dispatch]   accuracy  = verifying N given items; being wrong is the risk, not missing"
      echo "[dispatch]   judgment  = the verdict itself is the work (design/security/tradeoff)"
      echo "[dispatch] recent history: $0 --calibration"
    } >&2
    return 1
  fi
  case "$BOTTLENECK" in
    coverage|accuracy|judgment) ;;
    *) echo "[dispatch] invalid --bottleneck '$BOTTLENECK' (coverage|accuracy|judgment)" >&2; return 1 ;;
  esac
  # --why is free text ON TOP of the enum, never instead of it. A minimum
  # length blocks the "--why x" reflex; real audit pressure comes from the
  # verbatim ledger record (run.env/status.json), which the reader surfaces.
  if [[ -n "$TIER_WHY" && ${#TIER_WHY} -lt 10 ]]; then
    echo "[dispatch] --why too short ('$TIER_WHY'): one line of real reasoning (>=10 chars); it is recorded verbatim in the run ledger" >&2
    return 1
  fi
  if ! [[ "$TIMEOUT_MIN" =~ ^[0-9]+$ ]] || [[ "$TIMEOUT_MIN" -lt 1 ]]; then
    echo "[dispatch] invalid --timeout '$TIMEOUT_MIN' (positive minutes)" >&2
    return 1
  fi
  case "$NOTIFY" in
    push|silent) ;;
    *) echo "[dispatch] invalid --notify '$NOTIFY' (push|silent)" >&2; return 1 ;;
  esac
  if [[ -n "$WT_SLUG" ]] && ! [[ "$WT_SLUG" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "[dispatch] invalid --worktree slug '$WT_SLUG' ([A-Za-z0-9._-] only)" >&2
    return 1
  fi
  if [[ -n "$REPO_PATH" ]]; then
    if [[ -n "$WT_SLUG" ]]; then
      echo "[dispatch] --repo and --worktree are mutually exclusive: worktree mode already owns its cwd ($WORKTREE_BASE/<runid>-<slug>); --repo only redirects the READ-ONLY worker's cwd" >&2
      return 1
    fi
    if [[ ! -d "$REPO_PATH" ]]; then
      echo "[dispatch] --repo path is not a directory: $REPO_PATH" >&2
      return 1
    fi
    # Canonicalize: the worker cd's after detach, long after the caller's cwd
    # is gone -- a relative path must be pinned to an absolute one now.
    REPO_PATH="$(cd "$REPO_PATH" && pwd)"
    # Non-git dirs are ALLOWED (log-dir surveys are legitimate); warn only.
    if ! git -C "$REPO_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "[dispatch] warning: --repo path is not a git repository ($REPO_PATH) -- git commands will fail there; file reads still work"
    fi
  fi
  if [[ -n "$WT_REPO_ARG" ]]; then
    if [[ -z "$WT_SLUG" ]]; then
      echo "[dispatch] --worktree-repo requires --worktree <slug>: it only selects WHICH repo the worktree is cut from. For a read-only worker in another repo use --repo <path>." >&2
      return 1
    fi
    if [[ ! -d "$WT_REPO_ARG" ]]; then
      echo "[dispatch] --worktree-repo path is not a directory: $WT_REPO_ARG" >&2
      return 1
    fi
    # Canonicalize now: the worker resolves it after detach, when the
    # caller's cwd is long gone (same reason as --repo).
    WT_REPO_ARG="$(cd "$WT_REPO_ARG" && pwd)"
    # Unlike --repo, a git repo is MANDATORY here -- there is nothing to cut
    # a worktree from otherwise. Hard refusal, not a warning.
    if ! git -C "$WT_REPO_ARG" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "[dispatch] --worktree-repo path is not a git repository: $WT_REPO_ARG" >&2
      return 1
    fi
    WT_REPO="$WT_REPO_ARG"
  fi
  if [[ -n "$WT_BASE_ARG" && -z "$WT_SLUG" ]]; then
    echo "[dispatch] --base requires --worktree <slug>: it only selects the branch the worktree is cut from (a read-only worker has no branch)." >&2
    return 1
  fi
  return 0
}

# Resolve the base branch for the worktree (DGN-1184 (A)); see header. Sets
# WT_BASE. Explicit --base wins; else canonical marker + dev -> dev; else main.
resolve_wt_base() {
  local top
  if [[ -n "$WT_BASE_ARG" ]]; then
    WT_BASE="$WT_BASE_ARG"
    return 0
  fi
  top="$(git -C "$WT_REPO" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$WT_REPO")"
  if [[ -f "$top/.dogany-canonical" ]] \
     && git -C "$WT_REPO" show-ref --verify --quiet refs/heads/dev; then
    WT_BASE="dev"
  else
    WT_BASE="main"
  fi
}

# Provision dsp/<slug> worktree + freshness gate (DGN-556). Cut from WT_REPO
# (= WORKSPACE unless --worktree-repo moved it). Echoes nothing on stdout
# except via globals WT_PATH / BRANCH; returns 3 on failure. On success, the
# gate verdict (fresh / stale-merged) is recorded as one line in
# $RUNDIR/freshness.txt -- the machine's own record of whether the gate ran,
# so a worker or a later audit never has to guess (DGN-1230).
provision_worktree() {
  BRANCH="dsp/$WT_SLUG"
  # runid prefix keeps the path unique across repos as well as across runs.
  WT_PATH="$WORKTREE_BASE/$RUNID-$WT_SLUG"
  mkdir -p "$WORKTREE_BASE"
  resolve_wt_base
  # The base branch is the branch point AND the freshness reference -- a
  # target repo without it would fail deeper with an opaque "worktree add"
  # error.
  if ! git -C "$WT_REPO" show-ref --verify --quiet "refs/heads/$WT_BASE"; then
    echo "[dispatch] target repo has no '$WT_BASE' branch: $WT_REPO" >&2
    return 3
  fi
  if git -C "$WT_REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    if ! git -C "$WT_REPO" worktree add "$WT_PATH" "$BRANCH" >/dev/null 2>&1; then
      echo "[dispatch] worktree add failed (existing branch $BRANCH in $WT_REPO)" >&2
      return 3
    fi
  else
    if ! git -C "$WT_REPO" worktree add "$WT_PATH" -b "$BRANCH" "$WT_BASE" >/dev/null 2>&1; then
      echo "[dispatch] worktree add -b $BRANCH $WT_BASE failed (repo $WT_REPO)" >&2
      return 3
    fi
  fi
  # Freshness gate: the base must be an ancestor; stale -> merge base;
  # conflict -> abort (never build on a stale base).
  if git -C "$WT_PATH" merge-base --is-ancestor "$WT_BASE" HEAD; then
    echo "fresh base=$WT_BASE" > "$RUNDIR/freshness.txt"
  else
    if ! git -C "$WT_PATH" merge --no-edit "$WT_BASE" >/dev/null 2>&1; then
      git -C "$WT_PATH" merge --abort >/dev/null 2>&1 || true
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
      echo "[dispatch] freshness gate FAILED: branch $BRANCH cannot merge $WT_BASE cleanly -- resolve manually first" >&2
      return 3
    fi
    echo "stale-merged base=$WT_BASE" > "$RUNDIR/freshness.txt"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Observe mode (DGN-1191): kernel-enforced read-only for the non-worktree
# worker. Profile is RENDERED per run (paths substituted), SELF-CHECKED
# before spawn, and the worker only ever consumes it.
# ---------------------------------------------------------------------------

# Fail-closed availability gate. No allowlist fallback exists on purpose: an
# engine-level allowlist cannot see what an allowed command's options do
# (git --output, sqlite3 writefile, python3 -m json.tool ...), which is the
# hole this mode replaces.
observe_sandbox_available() {
  hash -r   # never trust a stale command hash for a fail-closed decision
  if ! command -v sandbox-exec >/dev/null 2>&1; then
    echo "[dispatch] observe mode needs sandbox-exec (macOS seatbelt) and it is not available -- REFUSING to spawn (no allowlist fallback: that fallback is the DGN-1191 write hole). Use --worktree <slug> for a write-capable run." >&2
    return 1
  fi
  return 0
}

# Seatbelt string literals: refuse paths the syntax cannot quote safely
# (fail-closed rather than emit a profile that parses differently).
sb_path_ok() {
  case "$1" in
    *'"'*|*'\'*|*$'\n'*|*$'\r'*) return 1 ;;
  esac
  [[ "$1" == /* ]]
}

# One (subpath ...) group covering the given path in every spelling the
# kernel might see: as given, its physical path (seatbelt compares canonical
# paths), and the /private form for a /tmp path that does not exist yet.
sb_subpaths() {
  local p="$1" r=""
  sb_path_ok "$p" || return 1
  printf '(subpath "%s")' "$p"
  if [[ -d "$p" ]]; then
    r="$(cd "$p" && pwd -P)"
    [[ "$r" != "$p" ]] && printf ' (subpath "%s")' "$r"
  elif [[ "$p" == /tmp/* || "$p" == /tmp ]]; then
    printf ' (subpath "/private%s")' "$p"
  fi
  return 0
}

# Escape a literal path for use inside a seatbelt (regex #"...") pattern.
sb_regex_escape() {
  printf '%s' "$1" | sed 's/[][\.*^$()|+?{}\\]/\\&/g'
}

# ~/.claude/projects/<slug> as the CLI derives it from the worker cwd.
sb_project_slug() {
  printf '%s' "$1" | sed 's/[^A-Za-z0-9]/-/g'
}

# render_observe_profile <out> <workdir> <session-id>
# Rule order is the contract: seatbelt applies the LAST matching rule, so the
# targets (WORKDIR / WORKSPACE / WORKTREE_BASE) are denied at the very end and
# beat the /tmp allow when a target lives under /tmp (the case that broke the
# first lab profile, docs/DGN-1191-observe-mode-design.md sec 7.1).
render_observe_profile() {
  local out="$1" workdir="$2" sid="$3"
  local home="$HOME" home_re cc slug slugs="" wd_real="" ws_real=""
  if ! sb_path_ok "$home" || ! sb_path_ok "$workdir" || ! sb_path_ok "$WORKSPACE" \
     || ! sb_path_ok "$WORKTREE_BASE" || ! [[ "$sid" =~ ^[0-9a-f-]{36}$ ]]; then
    echo "[dispatch] observe profile: unquotable path or bad session id -- refusing" >&2
    return 1
  fi
  home_re="$(sb_regex_escape "$home")"
  cc="$home/.claude"
  # git's keychain credential helper (https push). Resolved, not hardcoded;
  # absent helper = nothing to deny.
  local cred_helper=""
  cred_helper="$(git --exec-path 2>/dev/null)/git-credential-osxkeychain"
  { [[ -x "$cred_helper" ]] && sb_path_ok "$cred_helper"; } || cred_helper=""
  # Project-state slugs: the cwd as given and as the kernel reports it
  # (process.cwd() is physical) -- both are opened for THIS session only.
  slug="$(sb_project_slug "$workdir")"
  slugs="(literal \"$cc/projects/$slug\") (regex #\"^$home_re/\\.claude/projects/$slug/$sid(\\.|/|\$)\")"
  if [[ -d "$workdir" ]]; then
    wd_real="$(cd "$workdir" && pwd -P)"
    if [[ "$wd_real" != "$workdir" ]]; then
      slug="$(sb_project_slug "$wd_real")"
      slugs="$slugs (literal \"$cc/projects/$slug\") (regex #\"^$home_re/\\.claude/projects/$slug/$sid(\\.|/|\$)\")"
    fi
  fi
  {
    echo '(version 1)'
    echo ";; DGN-1191 observe mode -- rendered by dispatch-detached.sh; rule ORDER is load-bearing (last match wins)."
    echo '(allow default)'
    echo ';; 1. deny every filesystem write ...'
    echo '(deny file-write*)'
    echo ';; 2. ... then re-open only what the worker process itself needs'
    echo '(allow file-write* (subpath "/dev"))'
    echo '(allow file-write* (subpath "/private/var/folders"))   ; TMPDIR (mktemp of dry-run scripts)'
    echo '(allow file-write* (subpath "/private/tmp"))           ; /tmp scratch + CLI cwd tracker'
    echo "(allow file-write* (subpath \"$cc\"))                  ; CLI session state"
    echo ';; 3. inside ~/.claude: configuration, hooks, plugins and skills stay closed'
    echo "(deny file-write* (literal \"$cc/settings.json\") (literal \"$cc/settings.local.json\") (literal \"$cc/CLAUDE.md\") (literal \"$cc/keybindings.json\"))"
    echo "(deny file-write* (subpath \"$cc/plugins\") (subpath \"$cc/skills\") (subpath \"$cc/agents\") (subpath \"$cc/commands\") (subpath \"$cc/hooks\") (subpath \"$cc/scripts\") (subpath \"$cc/rules\"))"
    echo ';;    DGN-1050: the CLI writes ~/.claude/.credentials.json ONLY when a keychain write fails'
    echo ';;    non-transiently -- exactly what rule 1 causes -- and then DELETES the keychain entry.'
    echo ';;    Denying the file (regex: the CLI stages via .credentials.json.tmp.<hex>) makes that'
    echo ';;    fallback fail cleanly: audited CLI behavior is retry x3 -> save_failed, no crash, no'
    echo ';;    keychain delete, process continues on the old token. Reads stay open.'
    echo "(deny file-write* (regex #\"^$home_re/\\.claude/\\.credentials\\.json\"))"
    echo "(deny file-write* (regex #\"^$home_re/\\.claude\\.json\"))   ; global config incl. permission cache"
    echo ';; 4. state that OTHER live sessions source into their unsandboxed shells: shell snapshots'
    echo ';;    (denied outright -- the CLI runs without one), per-session env, transcripts (own session only)'
    echo "(deny file-write* (subpath \"$cc/shell-snapshots\"))"
    echo "(deny file-write* (subpath \"$cc/session-env\"))"
    echo "(allow file-write* (subpath \"$cc/session-env/$sid\"))"
    echo "(deny file-write* (subpath \"$cc/projects\"))"
    echo "(allow file-write* $slugs)"
    echo ';; 5. push credentials cut: ssh keys/known_hosts unreadable; the git credential helper cannot run'
    echo ';;    (denying the gitconfig files instead makes git itself fatal on EPERM -- measured)'
    echo "(deny file-read* (subpath \"$home/.ssh\") (subpath \"$home/.config/gh\"))"
    if [[ -n "$cred_helper" ]]; then
      echo "(deny file-read* (literal \"$cred_helper\"))"
      echo "(deny process-exec (literal \"$cred_helper\"))"
    fi
    echo ';; 6. TARGETS LAST -- these must beat every allow above, wherever the target lives'
    echo "(deny file-write* $(sb_subpaths "$workdir"))        ; WORKDIR (worker cwd)"
    echo "(deny file-write* $(sb_subpaths "$WORKSPACE"))      ; WORKSPACE (live checkout, RUNDIR, ledger)"
    echo "(deny file-write* $(sb_subpaths "$WORKTREE_BASE"))  ; other dispatches' worktrees"
  } > "$out" || return 1
  return 0
}

# observe_profile_selfcheck <profile> <target>...
# Proves the rendered profile before a worker exists: it compiles, scratch
# is open (the CLI cannot run otherwise), and a write inside EACH target is
# refused by the kernel. Any failure = spawn refusal (a render defect must
# never become an open worker).
observe_profile_selfcheck() {
  local prof="$1"; shift
  local t probe
  if ! sandbox-exec -f "$prof" /usr/bin/true 2>/dev/null; then
    echo "[dispatch] observe profile does not compile: $prof -- refusing to spawn" >&2
    return 1
  fi
  if ! sandbox-exec -f "$prof" /bin/sh -c 'd="$(mktemp -d)" && rmdir "$d"' 2>/dev/null; then
    echo "[dispatch] observe profile closed TMPDIR (the worker could not run) -- refusing to spawn" >&2
    return 1
  fi
  for t in "$@"; do
    [[ -d "$t" ]] || continue
    probe="$t/.observe-probe-$$"
    if sandbox-exec -f "$prof" /bin/mkdir "$probe" 2>/dev/null; then
      rmdir "$probe" 2>/dev/null || true
      echo "[dispatch] observe profile self-check FAILED: sandbox allowed a write inside $t -- refusing to spawn" >&2
      return 1
    fi
  done
  return 0
}

# select_tool_policy -> globals TOOL_ARGS (claude flags) and WRAP (command
# prefix). Worktree mode is byte-for-byte the pre-DGN-1191 policy.
select_tool_policy() {
  TOOL_ARGS=()
  WRAP=()
  if [[ -n "$WT_PATH" ]]; then
    TOOL_ARGS=(--permission-mode bypassPermissions --disallowedTools "Task,Agent")
    return 0
  fi
  if [[ -z "$SANDBOX_PROFILE" || ! -f "$SANDBOX_PROFILE" || -z "$SESSION_ID" ]]; then
    echo "[dispatch-worker] observe mode without a rendered sandbox profile -- refusing to run" >&2
    return 1
  fi
  observe_sandbox_available || return 1
  WRAP=(sandbox-exec -f "$SANDBOX_PROFILE")
  TOOL_ARGS=(--permission-mode default --allowedTools "Bash" \
    --disallowedTools "Task,Agent,Write,Edit,NotebookEdit" --session-id "$SESSION_ID")
  return 0
}

do_spawn() {
  validate_spawn_args || exit 1
  check_prompt_size || exit 1
  # DGN-1218: history first, verdict second -- when the gate below refuses,
  # the dispatcher re-decides with the calibration data already on screen.
  print_calibration || true
  bottleneck_gate || exit 2
  # Observe mode needs the kernel sandbox; refuse BEFORE any slot is held.
  if [[ -z "$WT_SLUG" ]]; then
    observe_sandbox_available || exit 2
  fi

  local rc=0
  run_gates || rc=$?
  if [[ $rc -ne 0 ]]; then
    exit 2
  fi
  # From here on the active-count slot is held: every failure path must decr.

  # Unique runid (self-grill G22: same-second collision would share a rundir).
  local tries=0
  while :; do
    RUNID="dsp-$(date +%Y%m%d-%H%M%S)-$(printf '%04d' $(( RANDOM % 10000 )))"
    RUNDIR="$DISPATCH_DIR/$RUNID"
    if mkdir "$RUNDIR" 2>/dev/null; then
      break
    fi
    tries=$(( tries + 1 ))
    if [[ $tries -ge 5 ]]; then
      echo "[dispatch] could not allocate a unique run dir under $DISPATCH_DIR" >&2
      active_count_decr
      exit 1
    fi
    sleep 1
  done

  WT_PATH=""
  BRANCH=""
  if [[ -n "$WT_SLUG" ]]; then
    local wrc=0
    provision_worktree || wrc=$?
    if [[ $wrc -ne 0 ]]; then
      active_count_decr
      rm -rf "$RUNDIR"
      exit 3
    fi
    LEDGER_BRANCH="$BRANCH"
    WORKDIR="$WT_PATH"
  else
    LEDGER_BRANCH="dsp/$RUNID"
    # --repo moves ONLY the worker cwd; every artifact path stays
    # WORKSPACE-anchored (see header).
    WORKDIR="${REPO_PATH:-$WORKSPACE}"
    # Observe mode: render the seatbelt profile into RUNDIR (under WORKSPACE,
    # so the sandboxed worker cannot edit its own cage) and prove it holds
    # against THIS run's targets before anything is spawned. A render defect
    # is a spawn refusal, never a silently open worker.
    SANDBOX_PROFILE="$RUNDIR/observe.sb"
    # The worker's CLI session id is minted HERE so the profile can open
    # exactly this session's ~/.claude state and nothing of any other session.
    SESSION_ID="$(uuidgen | tr 'A-Z' 'a-z')"
    if ! render_observe_profile "$SANDBOX_PROFILE" "$WORKDIR" "$SESSION_ID" \
       || ! observe_profile_selfcheck "$SANDBOX_PROFILE" "$WORKDIR" "$WORKSPACE"; then
      active_count_decr
      rm -rf "$RUNDIR"
      exit 2
    fi
  fi

  # Snapshot the prompt (caller file may move/change after detach).
  cp "$TASK_FILE" "$RUNDIR/prompt.md"

  # Serialized run parameters for the worker (printf %q -- quoting-safe).
  {
    printf 'RUNID=%q\n'         "$RUNID"
    printf 'LABEL=%q\n'         "$LABEL"
    printf 'TICKET=%q\n'        "$TICKET"
    printf 'MODEL=%q\n'         "$MODEL"
    printf 'BOTTLENECK=%q\n'    "$BOTTLENECK"
    printf 'TIER_WHY=%q\n'      "$TIER_WHY"
    printf 'RUNTIME=%q\n'       "$RUNTIME"
    printf 'WT_SLUG=%q\n'       "$WT_SLUG"
    printf 'REPO_PATH=%q\n'     "$REPO_PATH"
    printf 'WT_REPO=%q\n'       "$WT_REPO"
    printf 'WT_BASE=%q\n'       "$WT_BASE"
    printf 'WT_PATH=%q\n'       "$WT_PATH"
    printf 'BRANCH=%q\n'        "$BRANCH"
    printf 'LEDGER_BRANCH=%q\n' "$LEDGER_BRANCH"
    printf 'TIMEOUT_MIN=%q\n'   "$TIMEOUT_MIN"
    printf 'NOTIFY=%q\n'        "$NOTIFY"
    printf 'WORKDIR=%q\n'       "$WORKDIR"
    printf 'SANDBOX_PROFILE=%q\n' "$SANDBOX_PROFILE"
    printf 'SESSION_ID=%q\n'    "$SESSION_ID"
  } > "$RUNDIR/run.env"

  # Ledger running row FIRST (owner registration precedes the process, same
  # ordering as autonomous-loop). Note format is parser-compatible:
  # status-footer.py _LEDGER_MODEL_RE reads "model=<name>" from the note.
  if ! ensure_ledger; then
    active_count_decr
    if [[ -n "$WT_PATH" ]]; then
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
    fi
    rm -rf "$RUNDIR"
    exit 1
  fi
  local item="$LABEL"
  [[ -n "$TICKET" ]] && item="$LABEL [$TICKET]"
  if ! "$LEDGER_UPDATE" "$LEDGER_BRANCH" running "dispatch 스폰 (model=$MODEL axis=$BOTTLENECK)" \
        --item "$item" --ts "$(date '+%Y-%m-%d %H:%M %Z')"; then
    echo "[dispatch] ledger running-row write failed -- refusing to spawn an unowned worker" >&2
    active_count_decr
    if [[ -n "$WT_PATH" ]]; then
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
    fi
    rm -rf "$RUNDIR"
    exit 1
  fi

  # DGN-1012 terminal-state registration: one obligation per run. If this
  # wrapper never reaches finalize (SIGKILLed with its worker, host crash),
  # the hourly sweep expires the row and the owner agent receives the closure via the
  # session-inbox instead of silence. TTL = run timeout + 30min finalize
  # grace. Fail-open: a failed registration must never block a spawn.
  # (stderr NOT swallowed: a failing registration leaves a trace in this
  # wrapper's log instead of dying silently -- self-grill finding, L5.)
  python3 "$SCRIPT_DIR/terminal-state-ledger.py" open --surface dispatch \
    --id "$RUNID" --ttl "$(( (TIMEOUT_MIN + 30) * 60 ))" --notify session \
    --note "$item" --evidence "$RUNDIR/out.log" >/dev/null || true

  # Detach: python3 fork -> os.setsid -> exec (bridge/self_restart.sh:361-399
  # pattern, DGN-1010). Worker lands in its OWN session/pgid: unreachable by
  # the session CLI teardown AND by the launchd group cleanup.
  local detach_py=""
  if [[ -n "${BRIDGE_PYTHON:-}" ]]; then
    detach_py="$BRIDGE_PYTHON"
  elif command -v python3 >/dev/null 2>&1; then
    detach_py="python3"
  else
    echo "[dispatch] no python3 for setsid detach -- aborting (nohup fallback would die with the caller group)" >&2
    "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed "dispatch spawn failed (no python3)" || true
    active_count_decr
    if [[ -n "$WT_PATH" ]]; then
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
    fi
    exit 1
  fi

  local self worker_log worker_pid
  self="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
  worker_log="$RUNDIR/worker.log"
  worker_pid="$("$detach_py" - "$worker_log" "$self" --worker-run "$RUNDIR" <<'PYEOF'
import os, sys
log_path, target = sys.argv[1], sys.argv[2]
if os.fork() > 0:
    os._exit(0)  # launcher-side parent: return control to the caller now
os.setsid()      # own session: session-CLI/launchd group teardown cannot reach us
print(os.getpid(), flush=True)  # daemon pid -> launcher capture; releases the pipe
devnull = os.open(os.devnull, os.O_RDONLY)
log = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
os.dup2(devnull, 0)
os.dup2(log, 1)
os.dup2(log, 2)
os.close(devnull)
os.close(log)
os.execv(target, [target] + sys.argv[3:])
PYEOF
)"
  printf '%s\n' "${worker_pid:-}" > "$RUNDIR/pid"

  echo "[dispatch] spawned: runid=$RUNID pid=${worker_pid:-?} (own session via setsid)"
  echo "[dispatch]   label:  $item"
  echo "[dispatch]   model:  $MODEL  bottleneck: $BOTTLENECK  runtime: $RUNTIME  timeout: ${TIMEOUT_MIN}m  notify: $NOTIFY"
  echo "[dispatch]   ledger: $LEDGER_BRANCH (running)"
  [[ -n "$WT_PATH" ]] && echo "[dispatch]   worktree: $WT_PATH (branch $BRANCH, base $WT_BASE, repo $WT_REPO)"
  [[ -n "$REPO_PATH" ]] && echo "[dispatch]   repo:   $REPO_PATH (observe-mode worker cwd)"
  [[ -n "$SANDBOX_PROFILE" ]] && echo "[dispatch]   observe: kernel read-only (seatbelt $SANDBOX_PROFILE)"
  echo "[dispatch]   logs:   $RUNDIR/out.log"
  echo "[dispatch]   cancel: $self --cancel $RUNID"

  # DGN-1012 M2: terminal-state SWEEP DRIVE (leg 2 of 2).
  # Leg 1 (cron-guard.sh) rides every periodic job; it dies with launchd.
  # This leg rides delegation activity instead, so the two legs share no
  # single cause -- and this is the leg that covers exactly the window where
  # the other one is least trustworthy: the host that just came back up and
  # whose cron fleet has not fired yet, but on which someone is dispatching.
  #
  # Placed AFTER the worker is detached and reported, so the spawn path pays
  # nothing: throttled, this is one file read; unthrottled, the worker is
  # already running while it sweeps. Fail-open, as every registration is.
  python3 "$SCRIPT_DIR/terminal-state-ledger.py" sweep \
    --throttle "${DOGANY_TSL_SWEEP_INTERVAL:-3600}" >/dev/null || true
}

# ---------------------------------------------------------------------------
# Worker (runs detached in its own session; entry: --worker-run <rundir>)
# ---------------------------------------------------------------------------

# Backstop system prompt: autonomous-loop contract promoted (spec step 5).
# NOTE: the model must NEVER notify by itself -- push is issued by this shell
# only ("LLM judges, shell writes", spec R4).
worker_backstop() {
  cat <<'EOF'
You are a detached background worker dispatched by dispatch-detached.sh.
Hard rules (violations void the run):
- NEVER run git push, git tag, or any remote/network-mutating operation.
- NEVER edit baseline docs: AGENT.md, RULES.md, USER.md, rules/hot.framework.md, identity/ (agent + owner files), any SKILL.md, memories/.
- NEVER write to worklog/ and NEVER merge or commit to the main branch, nor
  to dev (the integration branch) -- merging your branch is the dispatcher's act.
- NEVER spawn sub-agents (Task/Agent are disabled).
- NEVER call routines/push.sh or send any user notification yourself.
  All notifications are issued by the dispatch wrapper shell after you exit.
  Your output is files only: in worktree mode, commit your work to the
  current branch; in observe mode, print your final report as plain text
  (the wrapper captures stdout to a log).
- In observe mode (no worktree) every file write outside TMPDIR and /tmp is
  denied by the kernel ("Operation not permitted"). That is the contract,
  not a bug: do not route around it (no launchctl / osascript / open / other
  out-of-sandbox executors). Bash is fully open: git -C, cd, sqlite3,
  python3, --dry-run scripts all work; scratch space is $TMPDIR. If the task
  needs writes, report back and ask the dispatcher to relaunch you with
  --worktree <slug> [--worktree-repo <path>].
- Do not restart/stop/reconfigure bots, services, or the bridge.
EOF
}

# JSON status writer -- python3 for quoting safety; deterministic shell owns
# the values, python only serializes.
write_status_json() {
  # args: result exit_code notify
  ST_RESULT="$1" ST_EXIT="$2" ST_NOTIFY="$3" \
  ST_RUNID="$RUNID" ST_LABEL="$LABEL" ST_TICKET="$TICKET" ST_MODEL="$MODEL" \
  ST_AXIS="${BOTTLENECK:-}" ST_RUNTIME="${RUNTIME:-}" ST_WHY="${TIER_WHY:-}" \
  ST_MODEL_OBSERVED="${MODEL_OBSERVED_JSON:-[]}" \
  ST_BRANCH="$BRANCH" ST_COMMITS="${COMMIT_COUNT:-0}" ST_HEAD="${COMMIT_HASH:-}" \
  ST_STARTED="${STARTED_AT:-}" ST_TIMEOUT="$TIMEOUT_MIN" \
  ST_OUTLOG="$RUNDIR/out.log" ST_INBOX="${INBOX_STATE:-pending}" \
  python3 - "$RUNDIR/status.json" <<'PY'
import json, os, sys, time

try:
    model_observed = json.loads(os.environ.get("ST_MODEL_OBSERVED", "[]") or "[]")
    if not isinstance(model_observed, list):
        model_observed = []
except Exception:
    model_observed = []

doc = {
    "runid":      os.environ.get("ST_RUNID", ""),
    "label":      os.environ.get("ST_LABEL", ""),
    "ticket":     os.environ.get("ST_TICKET", ""),
    "model":      os.environ.get("ST_MODEL", ""),
    # DGN-1218 measurement fields: the declared bottleneck axis (Layer 1
    # answer), the runtime/provider that executed, and the override
    # rationale if any. Without runtime, cross-provider comparison is
    # impossible once a second runtime exists.
    "bottleneck": os.environ.get("ST_AXIS", ""),
    "runtime":    os.environ.get("ST_RUNTIME", ""),
    "tier_why":   os.environ.get("ST_WHY", ""),
    # DGN-1227: alias's real version(s), observed from stream.jsonl at close
    # time. [] means "not observed" (no stream, unparseable, or genuinely no
    # assistant turn ran) -- never treat [] as "confirmed empty".
    "model_observed": model_observed,
    "result":     os.environ.get("ST_RESULT", ""),
    "exit_code":  int(os.environ.get("ST_EXIT", "0") or 0),
    "branch":     os.environ.get("ST_BRANCH", ""),
    "commits":    int(os.environ.get("ST_COMMITS", "0") or 0),
    "head":       os.environ.get("ST_HEAD", ""),
    "started_at": os.environ.get("ST_STARTED", ""),
    "ended_at":   time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "timeout_min": int(os.environ.get("ST_TIMEOUT", "0") or 0),
    "notify":     os.environ.get("ST_NOTIFY", ""),
    "out_log":    os.environ.get("ST_OUTLOG", ""),
    # DGN-1012: session-inbox drop trace -- "failed" here is the ONLY
    # surviving evidence when the Metal return path could not be written
    # (the drop itself is || true so it never breaks finalize).
    "session_inbox": os.environ.get("ST_INBOX", ""),
}
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
os.replace(tmp, sys.argv[1])
PY
}

# DGN-1012 return path: drop a completion file into the bridge session-inbox
# (bot.py _session_inbox_loop, DGN-217 -- same machinery as restart-verify /
# handoff / ticket-hygiene) so the METAL SESSION itself learns the run ended
# and picks the result up. The owner push below is for 형님's awareness only;
# without this drop, Metal stays blind until the owner manually asks
# (2026-08-24 incident: three survey dispatches completed, owner was pinged,
# Metal never was).
#
# NOTIFY semantics (do not conflate): --notify silent means "do not push the
# OWNER"; it does NOT mean "leave Metal uninformed". The inbox drop therefore
# runs UNCONDITIONALLY on every terminal state.
#
# UTF-8 contract: bot.py quarantines undecodable inbox files (bot.py:644),
# which would blind Metal all over again -- so the file is written by python3
# with encoding="utf-8" + errors="replace" (mangled label bytes degrade to
# U+FFFD instead of poisoning the drop). Temp-write + atomic rename per the
# DGN-217 writer contract; the dot-prefixed temp never matches the poller's
# *.md glob, so a half-written file is never injected.
write_session_inbox() {
  # args: result exit_code
  IB_RESULT="$1" IB_EXIT="$2" \
  IB_RUNID="$RUNID" IB_LABEL="$LABEL" IB_TICKET="$TICKET" IB_MODEL="$MODEL" \
  IB_BRANCH="$BRANCH" IB_COMMITS="${COMMIT_COUNT:-0}" IB_HEAD="${COMMIT_HASH:-}" \
  IB_OUTLOG="$RUNDIR/out.log" IB_INBOX="$SESSION_INBOX_DIR" \
  python3 - <<'PY'
# json is required by _stream_signals() below. It was missing until 2026-09-08
# (DGN-1347): every detached run's return-path drop died with
# "NameError: name 'json' is not defined" AFTER the work had completed, so the
# owning agent never learned the run ended. Do not remove either import.
import json
import os

def env(k):
    # os.environ decodes argv bytes with surrogateescape; surrogates must not
    # reach the file or bot.py quarantines it. Round-trip with replace so the
    # output ALWAYS decodes as clean UTF-8.
    return os.environ.get(k, "").encode("utf-8", "replace").decode("utf-8", "replace")

runid, label, ticket = env("IB_RUNID"), env("IB_LABEL"), env("IB_TICKET")
result, exit_code, model = env("IB_RESULT"), env("IB_EXIT"), env("IB_MODEL")
branch, head, outlog = env("IB_BRANCH"), env("IB_HEAD"), env("IB_OUTLOG")
try:
    commits = int(env("IB_COMMITS"))
except ValueError:
    commits = 0
inbox = os.environ["IB_INBOX"]

lines = [
    f"[dispatch-return] 디스패치 종결: {label or runid} -- {result}",
    "",
    f"- runid: {runid}",
    f"- label: {label}",
    f"- ticket: {ticket or '(없음)'}",
    f"- model: {model}",
    f"- result: {result} (exit={exit_code})",
]
if branch and commits > 0:
    lines.append(f"- branch: {branch} (commits={commits}, HEAD={head})")
lines += [f"- out.log: {outlog}", ""]
# DGN-1012 state/content split. LIFECYCLE (how the run ended) and PRODUCT
# (what it left behind) are two independent facts and must be measured from
# two independent sources: lifecycle from the runner's exit code, product from
# artifacts on disk. Binding them to one signal is the 2026-08-28 failure --
# the deadline killed a run (exit=124) whose work had already landed, and the
# drop told Metal to "diagnose the cause and consider re-dispatching" work
# that was sitting committed on a branch. Never infer product from lifecycle.
try:
    outlog_bytes = os.path.getsize(outlog)
except OSError:
    outlog_bytes = 0
# DGN-1337: byte count is NOT product. Session bootstrap alone writes 1-13KB
# (hook lines, init, thinking) with zero tool_use -- measured on two cancelled
# runs by Skull 2026-09-08. Treating bytes as product told the agent to
# "recover the artifact" from runs that produced nothing, twice. Same defect
# family as DGN-1331 B-branch: text exists -> work happened.
# Product now needs a real trace: a commit, a tool call, or agent speech.
def _stream_signals(run_dir):
    """(tool_use_count, assistant_msg_count) from stream.jsonl. Unreadable -> (0, 0)."""
    tu = am = 0
    path = os.path.join(run_dir, "stream.jsonl")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                blob = json.dumps(obj)
                if '"tool_use"' in blob:
                    tu += 1
                if obj.get("type") == "assistant":
                    am += 1
    except OSError:
        pass
    return tu, am

tool_uses, assistant_msgs = _stream_signals(os.path.dirname(outlog))
product = ("present" if (commits > 0 or tool_uses > 0 or assistant_msgs > 0)
           else "absent")
lines.insert(-1, f"- product: {product} "
                 f"(commits={commits}, tool_use={tool_uses}, "
                 f"assistant={assistant_msgs}, out.log={outlog_bytes}B)")

# Session-directive prefix, written ONCE: the four branches below differ in
# their instruction, not in who they address.
D = "Metal 지시: "

if product == "present":
    if commits > 0:
        recover = (f"branch {branch} 에 커밋 {commits}건이 남아 있다. "
                   "먼저 회수해 리뷰하고 머지/반려를 결정하라.")
    else:
        recover = "out.log 에 산출물이 남아 있다. 먼저 읽고 반영하라."
    if result == "completed":
        lines.append(f"{D}이 결과를 회수해 후속 작업을 진행하라 -- {recover}")
    else:
        # The kill and the product are BOTH true. Recovery leads; re-dispatch is
        # a decision made AFTER reading what already landed, never before.
        lines.append(
            f"{D}이 디스패치는 {result}(exit={exit_code}) 로 죽었지만 "
            f"산출물은 남아 있다 -- 재발주 판단 전에 회수가 먼저다. {recover} "
            "그 다음에 out.log 로 어디서 끊겼는지 확인하고 잔여분만 재발주하라.")
elif result == "completed":
    lines.append(
        f"{D}정상 종료했으나 산출물이 없다 -- 커밋도 out.log 내용도 "
        "없다. no-op 완료를 의심하고 발주 내용과 대조하라.")
else:
    lines.append(
        f"{D}이 디스패치는 {result}(exit={exit_code}) 로 끝났고 "
        "산출물도 없다. out.log 를 확인해 원인을 파악하고 재발주/수동 마무리 "
        "여부를 판단하라.")

os.makedirs(inbox, exist_ok=True)
name = f"dispatch-{runid}.md"
tmp = os.path.join(inbox, f".{name}.tmp")
with open(tmp, "w", encoding="utf-8", errors="replace") as f:
    f.write("\n".join(lines) + "\n")
os.replace(tmp, os.path.join(inbox, name))
PY
}

# DGN-1227: observe the REAL model identifier(s) claude -p actually ran, read
# from stream.jsonl -- run.env/status.json "model" is only ever the ALIAS
# (fable/opus/sonnet/haiku); we do not pin the alias->version mapping, so it
# can move between two dispatches while the ledger shows the same name both
# times (worklog/DGN-1227). This is an OBSERVATION, never a gate: parsing is
# defensive line-by-line (stream schema is not ours to depend on) and every
# failure mode -- missing file, empty file, malformed line, unexpected shape
# -- degrades to "[]" rather than a non-zero exit, so it can never block
# finalize. Multiple distinct values are ALL kept in first-seen order rather
# than collapsed to one, so a mid-stream version change (or a demotion, see
# fable_exhausted) is visible instead of silently averaged away. Layer 2
# (DGN-1226): a dev-agent ledger row, machine-observed, not user-facing --
# no layer-conditional branching lives here.
observe_model_from_stream() {
  local stream="$1"
  python3 - "$stream" <<'PY' 2>/dev/null || echo '[]'
import json, sys

path = sys.argv[1]
seen = []


def add(model):
    if isinstance(model, str) and model and model not in seen:
        seen.append(model)


def scan(want_type, want_subtype=None):
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except Exception:
                    continue
                if not isinstance(doc, dict) or doc.get("type") != want_type:
                    continue
                if want_subtype is not None:
                    if doc.get("subtype") != want_subtype:
                        continue
                    add(doc.get("model"))
                else:
                    msg = doc.get("message")
                    if isinstance(msg, dict):
                        add(msg.get("model"))
    except Exception:
        pass


scan("assistant")
if not seen:
    # Fallback: no assistant turn ever landed (e.g. killed before the first
    # response) -- system/init still names the model it was about to run.
    scan("system", want_subtype="init")
print(json.dumps(seen))
PY
}

# Finalize: idempotent (atomic .finalized guard -- cancel fallback and worker
# trap may race). Order per spec step 7: status.json -> worktree remove ->
# active-count decr -> ledger end row -> inbox drop (DGN-1012) -> push ->
# notify-failed backfill.
finalize_run() {
  # args: result exit_code
  local result="$1" exit_code="$2"
  mkdir "$RUNDIR/.finalized" 2>/dev/null || return 0

  COMMIT_COUNT=0
  COMMIT_HASH=""
  if [[ -n "$WT_PATH" && -d "$WT_PATH" ]]; then
    COMMIT_COUNT="$(git -C "$WT_PATH" rev-list --count "${WT_BASE:-main}..HEAD" 2>/dev/null || echo 0)"
    COMMIT_HASH="$(git -C "$WT_PATH" rev-parse --short HEAD 2>/dev/null || echo '')"
  fi

  # DGN-1227 (b) autofill -- observed at CLOSE time, not spawn time: run.env's
  # MODEL only ever knows the alias asked for; the real version behind it is
  # only knowable once the worker has actually produced output. See
  # observe_model_from_stream for the fail-open contract.
  MODEL_OBSERVED_JSON="$(observe_model_from_stream "$RUNDIR/stream.jsonl" 2>/dev/null)"
  [[ -n "$MODEL_OBSERVED_JSON" ]] || MODEL_OBSERVED_JSON='[]'
  local notify_state="pending"
  [[ "$NOTIFY" == "silent" ]] && notify_state="skipped-silent"
  write_status_json "$result" "$exit_code" "$notify_state" || true

  if [[ -n "$WT_PATH" ]]; then
    git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
  fi

  active_count_decr || true

  # Ledger end row -- existing state enum only (spec R2: no new states).
  local end_ts
  end_ts="$(date '+%Y-%m-%d %H:%M %Z')"
  case "$result" in
    completed)
      if [[ "$COMMIT_COUNT" -gt 0 ]]; then
        "$LEDGER_UPDATE" "$LEDGER_BRANCH" pending-review \
          "dispatch 완료 (commits=$COMMIT_COUNT, HEAD=$COMMIT_HASH)" --ts "$end_ts" || true
      else
        "$LEDGER_UPDATE" "$LEDGER_BRANCH" deferred \
          "dispatch 보고서형 완료 (out.log)" --flag dsp-report --ts "$end_ts" || true
      fi
      ;;
    timed-out)
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed \
        "dispatch timeout (${TIMEOUT_MIN}m 초과)" --ts "$end_ts" || true
      ;;
    canceled)
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed \
        "dispatch canceled" --ts "$end_ts" || true
      ;;
    *)
      "$LEDGER_UPDATE" "$LEDGER_BRANCH" failed \
        "dispatch 실패 (exit=$exit_code)" --ts "$end_ts" || true
      ;;
  esac

  # DGN-1012: Metal return path -- unconditional (see write_session_inbox
  # header: silent gates the OWNER push below, never this drop). A failed
  # drop must not break the exit path, but it may not fail silently either:
  # the "failed" trace lands in status.json (session_inbox field).
  INBOX_STATE="dropped"
  if write_session_inbox "$result" "$exit_code"; then
    # DGN-1012 terminal-state registration: the inbox drop IS the terminal
    # signal, so the obligation opened at spawn closes with it. A FAILED drop
    # deliberately leaves the obligation open -- the hourly sweep then expires
    # it and the owner agent still learns the run ended (silence-proof ordering).
    local _tsl_state="failed"
    [[ "$result" == "completed" ]] && _tsl_state="done"
    python3 "$SCRIPT_DIR/terminal-state-ledger.py" close --id "$RUNID" \
      --state "$_tsl_state" --note "dispatch $result (exit=$exit_code)" \
      --evidence "$RUNDIR/out.log" >/dev/null || true
  else
    INBOX_STATE="failed"
  fi
  write_status_json "$result" "$exit_code" "$notify_state" || true

  # Push: issued by THIS shell, never by the model (spec R4).
  if [[ "$NOTIFY" != "silent" ]]; then
    # Completion push copy below is a PLACEHOLDER -- 문구 미확정(형님 확인 대기),
    # dec-094 UX gate. Do not treat this wording as final.
    local push_text="dispatch ${result}: ${LABEL}${TICKET:+ [$TICKET]} (runid ${RUNID})"
    if [[ "$COMMIT_COUNT" -gt 0 ]]; then
      push_text="$push_text -- branch $BRANCH, commits $COMMIT_COUNT"
    fi
    if "$PUSH_SH" --text "$push_text"; then
      write_status_json "$result" "$exit_code" "sent" || true
    else
      write_status_json "$result" "$exit_code" "notify-failed" || true
    fi
  fi
  return 0
}

# bash-level timeout fallback (macOS ships no timeout(1)): job control gives
# the child its OWN pgid (set -m), so the watchdog can TERM the claude tree
# without killing this worker. Child pgid is recorded for --cancel.
# Returns child's exit code; 124 on timeout.
# DGN-1192: render out.log from the FULL stream.
# Contract: out.log stays the single artifact every downstream consumer reads
# (status.json out_log, session-inbox note, size check). Only its PRODUCER
# changes -- last-message capture -> concatenation of every assistant text
# block in order. Thinking blocks are excluded (not report content).
# Fail-open by design: any parse problem falls back to the raw stream + stderr
# so a rendering bug can never destroy evidence -- that is the very defect
# this function exists to remove.
render_out_log() {
  local stream="$RUNDIR/stream.jsonl" err="$RUNDIR/claude.err" out="$RUNDIR/out.log"
  [[ -f "$stream" ]] || { : > "$out"; [[ -s "$err" ]] && cat "$err" >> "$out"; return 0; }
  if ! RO_STREAM="$stream" RO_OUT="$out" python3 - <<'PY_RENDER'
import json, os, sys
src, dst = os.environ["RO_STREAM"], os.environ["RO_OUT"]
parts, seen_any = [], False
with open(src, "r", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except Exception:
            continue
        if doc.get("type") != "assistant":
            continue
        msg = doc.get("message")
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text") or ""
                if text.strip():
                    parts.append(text)
                    seen_any = True
if not seen_any:
    sys.exit(3)
with open(dst, "w") as fh:
    fh.write("\n\n".join(parts).rstrip() + "\n")
PY_RENDER
  then
    # No assistant text recovered (or renderer failed): keep EVERYTHING.
    {
      echo "[dispatch] out.log render found no assistant text -- raw stream follows (DGN-1192 fallback)"
      cat "$stream"
      [[ -s "$err" ]] && { echo; echo "--- stderr ---"; cat "$err"; }
    } > "$out"
    return 0
  fi
  if [[ -s "$err" ]]; then
    { echo; echo "--- stderr ---"; cat "$err"; } >> "$out"
  fi
  return 0
}

run_claude_with_deadline() {
  local secs="$1"; shift
  local timeout_bin=""
  if command -v timeout >/dev/null 2>&1; then
    timeout_bin="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then
    timeout_bin="gtimeout"
  fi

  local rc=0
  if [[ -n "$timeout_bin" ]]; then
    ( cd "$WORKDIR" && "$timeout_bin" "${secs}s" "$@" ) \
      < /dev/null > "$RUNDIR/stream.jsonl" 2> "$RUNDIR/claude.err" || rc=$?
    return $rc
  fi

  set -m
  ( cd "$WORKDIR" && "$@" ) < /dev/null > "$RUNDIR/stream.jsonl" 2> "$RUNDIR/claude.err" &
  local cpid=$!
  set +m
  printf '%s\n' "$cpid" > "$RUNDIR/claude.pid"

  local waited=0
  while kill -0 "$cpid" 2>/dev/null; do
    if [[ $waited -ge $secs ]]; then
      kill -TERM -- "-$cpid" 2>/dev/null || true
      sleep 5
      kill -KILL -- "-$cpid" 2>/dev/null || true
      wait "$cpid" 2>/dev/null || true
      rm -f "$RUNDIR/claude.pid"
      return 124
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  wait "$cpid" || rc=$?
  rm -f "$RUNDIR/claude.pid"
  return $rc
}

# EXIT backstop (self-grill G9): if the worker shell dies early (set -e trip,
# missing file, unexpected error) BEFORE the normal finalize, the run would
# leak (running ledger row + held count slot) with no owner. The .finalized
# guard makes this a no-op on every healthy path.
worker_on_exit() {
  if [[ -n "${RUNDIR:-}" && -d "$RUNDIR" && ! -d "$RUNDIR/.finalized" ]]; then
    finalize_run error 1 || true
  fi
}

worker_on_term() {
  # Cancel path: reap the claude subtree (own pgid in fallback mode), then
  # finalize as canceled. Idempotent via finalize guard.
  if [[ -f "$RUNDIR/claude.pid" ]]; then
    local cpid
    cpid="$(cat "$RUNDIR/claude.pid" 2>/dev/null || echo '')"
    if [[ -n "$cpid" ]]; then
      kill -TERM -- "-$cpid" 2>/dev/null || true
      sleep 2
      kill -KILL -- "-$cpid" 2>/dev/null || true
    fi
  fi
  render_out_log || true   # DGN-1192: cancel path must keep partial evidence
  finalize_run canceled 143
  exit 143
}

do_worker() {
  RUNDIR="$WORKER_RUNDIR"
  if [[ ! -d "$RUNDIR" || ! -f "$RUNDIR/run.env" ]]; then
    echo "[dispatch-worker] bad rundir: $RUNDIR" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$RUNDIR/run.env"

  printf '%s\n' "$$" > "$RUNDIR/pid"
  # Spawn-time process identity for --cancel pid-reuse verification (spec R5).
  ps -o lstart=,command= -p "$$" > "$RUNDIR/proc.txt" 2>/dev/null || true
  STARTED_AT="$(date +%Y-%m-%dT%H:%M:%S%z)"

  trap worker_on_exit EXIT
  trap worker_on_term TERM

  local prompt backstop
  prompt="$(cat "$RUNDIR/prompt.md")"
  backstop="$(worker_backstop)"

  # Tool policy + execution wrapper: worktree mode = full build perms (loop
  # parity, unchanged); observe mode = kernel sandbox + open Bash (header
  # "OBSERVE MODE"). The worker never re-renders the profile: it was rendered
  # and self-checked by the launcher and lives under WORKSPACE, which the
  # sandbox denies.
  if ! select_tool_policy; then
    finalize_run error 2
    exit 2
  fi

  # -u CLAUDECODE: nested claude -p is rejected inside a session env.
  # -u PROJECT_ROOT: child usage-gate misclassification guard (spec step 5).
  local claude_rc=0
  run_claude_with_deadline "$(( TIMEOUT_MIN * 60 ))" \
    ${WRAP[@]+"${WRAP[@]}"} env -u CLAUDECODE -u PROJECT_ROOT claude -p "$prompt" \
      --model "$MODEL" \
      --append-system-prompt "$backstop" \
      --output-format stream-json --verbose \
      "${TOOL_ARGS[@]}" \
    || claude_rc=$?

  # DGN-1192: out.log is RENDERED from the full stream, not captured as the
  # last message. `claude -p` in text mode prints only the FINAL assistant
  # message; a worker that split its report across two messages lost the
  # first one silently, and a truncated report always reads as "this model
  # was not good enough" -- poisoning DGN-1193 tier measurement upward.
  render_out_log

  trap - TERM

  if [[ $claude_rc -eq 124 ]]; then
    finalize_run timed-out 124
  elif [[ $claude_rc -eq 0 ]]; then
    finalize_run completed 0
  else
    # DGN-1158: this dispatch died -- if it was fable's weekly-cap death,
    # write the flag so the NEXT fable spawn's run_gates() demotes to opus.
    # No auto-retry of THIS run.
    if is_fable_credits_death "$RUNDIR/out.log"; then
      echo "[dispatch-worker] fable weekly cap exhausted (out.log: requires usage credits) -> recording $FABLE_FLAG so the next fable spawn demotes (DGN-1158)"
      write_fable_exhausted_marker || echo "[dispatch-worker] failed to write $FABLE_FLAG (non-fatal)" >&2
    fi
    finalize_run error "$claude_rc"
  fi
}

# ---------------------------------------------------------------------------
# Cancel: verify process identity (pid-reuse guard, spec R5) -> TERM the
# worker's process group -> 5s grace -> KILL. Worker's TERM trap does the
# orderly finalize; if it never runs, cancel finalizes from this side
# (idempotent via the .finalized guard).
# ---------------------------------------------------------------------------
do_cancel() {
  RUNDIR="$DISPATCH_DIR/$CANCEL_RUNID"
  if [[ ! -d "$RUNDIR" || ! -f "$RUNDIR/run.env" ]]; then
    echo "[dispatch] unknown runid: $CANCEL_RUNID" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$RUNDIR/run.env"

  if [[ -d "$RUNDIR/.finalized" ]]; then
    echo "[dispatch] $CANCEL_RUNID already finalized -- nothing to cancel"
    exit 0
  fi

  local pid=""
  [[ -f "$RUNDIR/pid" ]] && pid="$(cat "$RUNDIR/pid" 2>/dev/null | tr -d ' \n')"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    echo "[dispatch] worker not running (pid=${pid:-none}) -- finalizing bookkeeping only"
    finalize_run canceled 1
    exit 0
  fi

  # pid-reuse guard: the recorded spawn-time (lstart, command) must match the
  # live process before we send any signal (spec R5 -- never kill a stranger).
  local recorded current
  recorded="$(cat "$RUNDIR/proc.txt" 2>/dev/null | tr -s ' ' | sed 's/^ *//;s/ *$//')"
  current="$(ps -o lstart=,command= -p "$pid" 2>/dev/null | tr -s ' ' | sed 's/^ *//;s/ *$//')"
  if [[ -z "$recorded" || -z "$current" || "$recorded" != "$current" ]]; then
    echo "[dispatch] REFUSED: pid $pid identity mismatch (reused pid?)" >&2
    echo "  recorded: ${recorded:-<none>}" >&2
    echo "  current:  ${current:-<none>}" >&2
    exit 1
  fi

  # Worker is its own session/group leader (setsid) -> pgid == pid.
  echo "[dispatch] cancel $CANCEL_RUNID: TERM to process group $pid"
  kill -TERM -- "-$pid" 2>/dev/null || true

  local waited=0
  while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 5 ]]; do
    sleep 1
    waited=$(( waited + 1 ))
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "[dispatch] worker survived TERM -- escalating to KILL"
    kill -KILL -- "-$pid" 2>/dev/null || true
    # Fallback-timeout mode keeps claude in a separate pgid -- reap it too.
    if [[ -f "$RUNDIR/claude.pid" ]]; then
      local cpid
      cpid="$(cat "$RUNDIR/claude.pid" 2>/dev/null | tr -d ' \n')"
      if [[ -n "$cpid" ]]; then
        kill -KILL -- "-$cpid" 2>/dev/null || true
      fi
    fi
  fi

  # Worker trap normally finalizes; give it a moment, then backstop from here.
  sleep 2
  if [[ ! -d "$RUNDIR/.finalized" ]]; then
    finalize_run canceled 137
  fi
  echo "[dispatch] canceled: $CANCEL_RUNID"
}

# ---------------------------------------------------------------------------
# DGN-1227 (3) retroactive restore -- past runs whose stream.jsonl survived
# never got model_observed written (the feature did not exist yet). Reports
# candidates and, only with --apply, patches ONLY the missing field into
# status.json via the SAME observe_model_from_stream parser that finalize_run
# uses live. Default is dry-run -- this never runs unattended and never
# touches a status.json that already has the field or that has no
# stream.jsonl left to observe. Backfilled rows are tagged
# model_observed_backfilled=true so a reader can tell "observed live" from
# "reconstructed after the fact" apart (a backfilled run may have been
# canceled/killed mid-stream, so its stream is not necessarily as complete
# as a live-finalized one's).
# ---------------------------------------------------------------------------
do_backfill() {
  if [[ ! -d "$DISPATCH_DIR" ]]; then
    echo "[backfill] no dispatches yet"
    return 0
  fi
  local total=0 already=0 nostream=0 candidates=0 patched=0 failed=0
  local d runid status_file stream_file observed has_field
  for d in "$DISPATCH_DIR"/dsp-*/; do
    [[ -d "$d" ]] || continue
    status_file="${d}status.json"
    stream_file="${d}stream.jsonl"
    [[ -f "$status_file" ]] || continue
    total=$(( total + 1 ))
    runid="$(basename "$d")"
    has_field="$(python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print("1" if isinstance(d, dict) and "model_observed" in d else "0")
except Exception:
    print("0")
' "$status_file" 2>/dev/null || echo 0)"
    if [[ "$has_field" == "1" ]]; then
      already=$(( already + 1 ))
      continue
    fi
    if [[ ! -f "$stream_file" ]]; then
      nostream=$(( nostream + 1 ))
      continue
    fi
    candidates=$(( candidates + 1 ))
    observed="$(observe_model_from_stream "$stream_file" 2>/dev/null)"
    [[ -n "$observed" ]] || observed='[]'
    if [[ "$BACKFILL_APPLY" -eq 1 ]]; then
      if RB_OBSERVED="$observed" python3 - "$status_file" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
except Exception as e:
    print(f"[backfill] SKIP unreadable {path}: {e}", file=sys.stderr)
    sys.exit(1)
try:
    observed = json.loads(os.environ.get("RB_OBSERVED", "[]") or "[]")
    if not isinstance(observed, list):
        observed = []
except Exception:
    observed = []
doc["model_observed"] = observed
doc["model_observed_backfilled"] = True
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
      then
        patched=$(( patched + 1 ))
        echo "[backfill] APPLIED $runid -> $observed"
      else
        failed=$(( failed + 1 ))
        echo "[backfill] FAILED  $runid" >&2
      fi
    else
      echo "[backfill] WOULD-APPLY $runid -> $observed"
    fi
  done
  echo "[backfill] total=$total already_has_field=$already no_stream_left=$nostream candidates=$candidates"
  if [[ "$BACKFILL_APPLY" -eq 1 ]]; then
    echo "[backfill] patched=$patched failed=$failed"
  else
    echo "[backfill] dry-run -- pass --apply to write model_observed into these $candidates run(s)"
  fi
}

# ---------------------------------------------------------------------------
# List: one line per run dir -- state from status.json, else live pid check.
# ---------------------------------------------------------------------------
do_list() {
  if [[ ! -d "$DISPATCH_DIR" ]]; then
    echo "[dispatch] no dispatches yet"
    return 0
  fi
  local found=0 d runid state pid label
  for d in "$DISPATCH_DIR"/dsp-*/; do
    [[ -d "$d" ]] || continue
    found=1
    runid="$(basename "$d")"
    label=""
    if [[ -f "$d/run.env" ]]; then
      # run.env values are printf %q quoted -- unquote via subshell source.
      label="$( ( source "$d/run.env" 2>/dev/null && printf '%s' "${LABEL:-}" ) || true )"
    fi
    if [[ -f "$d/status.json" ]]; then
      state="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("result","?"))' "$d/status.json" 2>/dev/null || echo '?')"
    else
      pid="$(cat "$d/pid" 2>/dev/null | tr -d ' \n')"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        state="running (pid $pid)"
      else
        state="dead-unfinalized"
      fi
    fi
    printf '%s  %-22s  %s\n' "$runid" "$state" "$label"
  done
  if [[ $found -eq 0 ]]; then
    echo "[dispatch] no dispatches yet"
  fi
}

main() {
  case "$MODE" in
    spawn)       do_spawn ;;
    cancel)      do_cancel ;;
    list)        do_list ;;
    calibration) print_calibration ;;
    worker)      do_worker ;;
    backfill)    do_backfill ;;
  esac
}

# Sourcing guard for tests: DISPATCH_TEST=1 loads functions without executing.
if [[ -z "${DISPATCH_TEST:-}" ]]; then
  main
fi
