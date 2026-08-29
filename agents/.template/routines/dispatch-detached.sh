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
#     --label <label> [--ticket DGN-NNN] [--worktree <slug>] [--timeout <min, default 60>]
#     [--notify silent|push]   (default: silent -- dec-126, owner sees results not events)
#     [--repo <path>]   (read-only mode only: worker cwd -> <path>; excludes --worktree)
#     [--worktree-repo <path>]   (requires --worktree: provision the worktree in THAT
#                                 git repo instead of the workspace -- WRITE-capable
#                                 target repo; artifacts stay workspace-anchored)
#   dispatch-detached.sh --cancel <runid>
#   dispatch-detached.sh --list
#
# CONTRACTS (locked spec: worklog/DGN-991-stop-kills-background-work.md rev3):
#   - Gates run SYNCHRONOUSLY before detach: usage-gate.sh (fail-CLOSED),
#     .claude/.fable-exhausted pre-check (fable -> opus demotion, logged),
#     active-count cap 3, prompt size cap 100KB (ARG_MAX).
#   - active-count (.telegram_bot/dispatch/active-count) is a SEPARATE ledger
#     from autonomous-loop's DAILY_SPAWN_BUDGET / PER_RUN_CAP. The loop budget
#     governs queue juniors; this counter governs interactive detached
#     dispatches only. Never merge the two.
#   - --worktree <slug>: branch dsp/<slug> (namespace-separated from loop's
#     auto/*), freshness gate (main must be ancestor). WITHOUT --worktree the
#     worker is forced READ-ONLY (--allowedTools whitelist, no
#     bypassPermissions) -- blocks a second writer on the live checkout.
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
#     the TARGET repo (WT_REPO); the gate compares that repo's own main.
#   - --repo <path> (read-only mode only, mutually exclusive with --worktree):
#     sets the worker cwd to <path> so the pure-form scoped git grants
#     (Bash(git log:*) etc.) read THAT repo. Permission matching is literal-
#     prefix: "git -C X log" never matches "git log", and "cd X && git" is
#     blocked by the engine (untrusted-hooks guard) -- cwd is the ONLY lever
#     (DGN-1012, live-probed 2026-08-24). cwd is also the ONLY thing that
#     moves: RUNDIR / out.log / status.json / session-inbox drop / ledger /
#     active-count stay WORKSPACE-anchored absolute paths, and the read-only
#     tool policy (deny Write/Edit, scoped allow list) is unchanged. To WRITE
#     in another repo use --worktree <slug> --worktree-repo <path> instead.
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
# EXIT CODES (launcher): 0 spawned/ok, 1 usage/config error, 2 gate refused,
#   3 worktree/freshness failure.

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

# 2026-08-26 13:0x: 3 -> 5 (형님 "태워") -> 13:2x: 1 (7일 창 97% critical)
#   -> 13:3x: 3 복귀 (형님 계정 전환, 새 창 7d 1% 실측).
# 교훈(주석으로 남긴다): 상한을 올리기 전에 잔량을 먼저 실측한다. 13:0x 인상 시점에
#   확인 안 했고, 40분 만에 7일 창이 97%까지 갔다. usage-gate 는 임계에서 거부할 뿐
#   임계까지 가는 속도는 이 상수가 정한다 -- "게이트가 있으니 괜찮다"는 틀린 추론.
#   기본값 3 = 설계값. 5 이상은 잔량 실측 + 형님 승인 동반일 때만.
MAX_ACTIVE=3
PROMPT_MAX_BYTES=102400   # 100KB -- ARG_MAX guard (spec R8)
DEFAULT_TIMEOUT_MIN=60

# ---------------------------------------------------------------------------
# Argument parsing / mode routing
# ---------------------------------------------------------------------------
usage() {
  sed -n '12,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

MODE="spawn"
TASK_FILE=""
MODEL=""
LABEL=""
TICKET=""
WT_SLUG=""
REPO_PATH=""
WT_REPO_ARG=""
# Git repo the worktree is cut from. Default = workspace (legacy behaviour).
# Also the fallback when do_cancel sources a run.env written by an older
# version of this script (no WT_REPO line) -- set -u would trip otherwise.
WT_REPO="$WORKSPACE"
TIMEOUT_MIN="$DEFAULT_TIMEOUT_MIN"
# dec-126 (owner, 2026-08-24): default = silent. The owner is NOT notified that
# a dispatch finished -- Metal receives the completion via the session-inbox
# drop (unconditional, see write_session_inbox) and reports the RESULT in its
# own words. What reaches the owner should be the outcome, not the event.
# --notify push stays available for the rare run the owner is actually waiting on.
NOTIFY="silent"
CANCEL_RUNID=""
WORKER_RUNDIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --task)     TASK_FILE="$2"; shift 2 ;;
    --model)    MODEL="$2"; shift 2 ;;
    --label)    LABEL="$2"; shift 2 ;;
    --ticket)   TICKET="$2"; shift 2 ;;
    --worktree) WT_SLUG="$2"; shift 2 ;;
    --repo)     REPO_PATH="$2"; shift 2 ;;
    --worktree-repo) WT_REPO_ARG="$2"; shift 2 ;;
    --timeout)  TIMEOUT_MIN="$2"; shift 2 ;;
    --notify)   NOTIFY="$2"; shift 2 ;;
    --cancel)   MODE="cancel"; CANCEL_RUNID="$2"; shift 2 ;;
    --list)     MODE="list"; shift 1 ;;
    --worker-run) MODE="worker"; WORKER_RUNDIR="$2"; shift 2 ;;
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
# Spawn path
# ---------------------------------------------------------------------------
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
    # Non-git dirs are ALLOWED (log-dir surveys are legitimate); warn only,
    # because the scoped git grants will be useless there.
    if ! git -C "$REPO_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "[dispatch] warning: --repo path is not a git repository ($REPO_PATH) -- scoped git commands will fail there; file reads still work"
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
  return 0
}

# Provision dsp/<slug> worktree + freshness gate (DGN-556). Cut from WT_REPO
# (= WORKSPACE unless --worktree-repo moved it). Echoes nothing on stdout
# except via globals WT_PATH / BRANCH; returns 3 on failure.
provision_worktree() {
  BRANCH="dsp/$WT_SLUG"
  # runid prefix keeps the path unique across repos as well as across runs.
  WT_PATH="$WORKTREE_BASE/$RUNID-$WT_SLUG"
  mkdir -p "$WORKTREE_BASE"
  # main is the branch point AND the freshness reference -- a target repo
  # without it would fail deeper with an opaque "worktree add" error.
  if ! git -C "$WT_REPO" show-ref --verify --quiet refs/heads/main; then
    echo "[dispatch] target repo has no 'main' branch: $WT_REPO" >&2
    return 3
  fi
  if git -C "$WT_REPO" show-ref --verify --quiet "refs/heads/$BRANCH"; then
    if ! git -C "$WT_REPO" worktree add "$WT_PATH" "$BRANCH" >/dev/null 2>&1; then
      echo "[dispatch] worktree add failed (existing branch $BRANCH in $WT_REPO)" >&2
      return 3
    fi
  else
    if ! git -C "$WT_REPO" worktree add "$WT_PATH" -b "$BRANCH" main >/dev/null 2>&1; then
      echo "[dispatch] worktree add -b $BRANCH main failed (repo $WT_REPO)" >&2
      return 3
    fi
  fi
  # Freshness gate: main must be an ancestor; stale -> merge main; conflict ->
  # abort (never build on a stale base).
  if ! git -C "$WT_PATH" merge-base --is-ancestor main HEAD; then
    if ! git -C "$WT_PATH" merge --no-edit main >/dev/null 2>&1; then
      git -C "$WT_PATH" merge --abort >/dev/null 2>&1 || true
      git -C "$WT_REPO" worktree remove "$WT_PATH" --force >/dev/null 2>&1 || true
      echo "[dispatch] freshness gate FAILED: branch $BRANCH cannot merge main cleanly -- resolve manually first" >&2
      return 3
    fi
  fi
  return 0
}

do_spawn() {
  validate_spawn_args || exit 1
  check_prompt_size || exit 1

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
    # --repo moves ONLY the worker cwd (so pure-form scoped git reads that
    # repo); every artifact path stays WORKSPACE-anchored (see header).
    WORKDIR="${REPO_PATH:-$WORKSPACE}"
  fi

  # Snapshot the prompt (caller file may move/change after detach).
  cp "$TASK_FILE" "$RUNDIR/prompt.md"

  # Serialized run parameters for the worker (printf %q -- quoting-safe).
  {
    printf 'RUNID=%q\n'         "$RUNID"
    printf 'LABEL=%q\n'         "$LABEL"
    printf 'TICKET=%q\n'        "$TICKET"
    printf 'MODEL=%q\n'         "$MODEL"
    printf 'WT_SLUG=%q\n'       "$WT_SLUG"
    printf 'REPO_PATH=%q\n'     "$REPO_PATH"
    printf 'WT_REPO=%q\n'       "$WT_REPO"
    printf 'WT_PATH=%q\n'       "$WT_PATH"
    printf 'BRANCH=%q\n'        "$BRANCH"
    printf 'LEDGER_BRANCH=%q\n' "$LEDGER_BRANCH"
    printf 'TIMEOUT_MIN=%q\n'   "$TIMEOUT_MIN"
    printf 'NOTIFY=%q\n'        "$NOTIFY"
    printf 'WORKDIR=%q\n'       "$WORKDIR"
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
  if ! "$LEDGER_UPDATE" "$LEDGER_BRANCH" running "dispatch 스폰 (model=$MODEL)" \
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
  echo "[dispatch]   model:  $MODEL  timeout: ${TIMEOUT_MIN}m  notify: $NOTIFY"
  echo "[dispatch]   ledger: $LEDGER_BRANCH (running)"
  [[ -n "$WT_PATH" ]] && echo "[dispatch]   worktree: $WT_PATH (branch $BRANCH, repo $WT_REPO)"
  [[ -n "$REPO_PATH" ]] && echo "[dispatch]   repo:   $REPO_PATH (read-only worker cwd)"
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
- NEVER write to worklog/ and NEVER merge or commit to the main branch.
- NEVER spawn sub-agents (Task/Agent are disabled).
- NEVER call routines/push.sh or send any user notification yourself.
  All notifications are issued by the dispatch wrapper shell after you exit.
  Your output is files only: in worktree mode, commit your work to the
  current branch; in read-only mode, print your final report as plain text
  (the wrapper captures stdout to a log).
- In read-only mode, always invoke git in its PURE form ("git log",
  "git status", ...): "git -C <path>" and "cd <path> && git" are blocked by
  the permission engine. Pure-form git reads the repo at your cwd. If you
  need to inspect a DIFFERENT repository, do not work around the block --
  report back and ask the dispatcher to relaunch you with --repo <path>
  (read-only) or --worktree <slug> --worktree-repo <path> (write).
- Do not restart/stop/reconfigure bots, services, or the bridge.
EOF
}

# JSON status writer -- python3 for quoting safety; deterministic shell owns
# the values, python only serializes.
write_status_json() {
  # args: result exit_code notify
  ST_RESULT="$1" ST_EXIT="$2" ST_NOTIFY="$3" \
  ST_RUNID="$RUNID" ST_LABEL="$LABEL" ST_TICKET="$TICKET" ST_MODEL="$MODEL" \
  ST_BRANCH="$BRANCH" ST_COMMITS="${COMMIT_COUNT:-0}" ST_HEAD="${COMMIT_HASH:-}" \
  ST_STARTED="${STARTED_AT:-}" ST_TIMEOUT="$TIMEOUT_MIN" \
  ST_OUTLOG="$RUNDIR/out.log" ST_INBOX="${INBOX_STATE:-pending}" \
  python3 - "$RUNDIR/status.json" <<'PY'
import json, os, sys, time
doc = {
    "runid":      os.environ.get("ST_RUNID", ""),
    "label":      os.environ.get("ST_LABEL", ""),
    "ticket":     os.environ.get("ST_TICKET", ""),
    "model":      os.environ.get("ST_MODEL", ""),
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
product = "present" if (commits > 0 or outlog_bytes > 0) else "absent"
lines.insert(-1, f"- product: {product} "
                 f"(commits={commits}, out.log={outlog_bytes}B)")

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
    COMMIT_COUNT="$(git -C "$WT_PATH" rev-list --count main..HEAD 2>/dev/null || echo 0)"
    COMMIT_HASH="$(git -C "$WT_PATH" rev-parse --short HEAD 2>/dev/null || echo '')"
  fi

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
      < /dev/null > "$RUNDIR/out.log" 2>&1 || rc=$?
    return $rc
  fi

  set -m
  ( cd "$WORKDIR" && "$@" ) < /dev/null > "$RUNDIR/out.log" 2>&1 &
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

  # Tool policy: worktree mode = full build perms (loop parity); no worktree
  # = READ-ONLY whitelist, default permission mode (headless cannot grant, so
  # every non-whitelisted tool call is denied -> writes blocked).
  local -a tool_args
  if [[ -n "$WT_PATH" ]]; then
    tool_args=(--permission-mode bypassPermissions --disallowedTools "Task,Agent")
  else
    # Read-only hardening (self-grill G16): the allow whitelist alone can be
    # punched through by a settings-level allow rule (settings allowlists and
    # --allowedTools are additive). Deny beats allow, so write-capable tools
    # are explicitly denied on top of the whitelist.
    #
    # DGN-1012: "read-only" means WRITE-forbidden, not SHELL-forbidden. A
    # blanket Bash deny forced git-evidence surveys into hand-reading
    # .git/logs/HEAD (2026-08-24 incident, three workers self-reported "no
    # Bash" as their limiting factor). The shell is opened ONLY through
    # command-scoped allow rules (prefix syntax Bash(git log:*), documented
    # and verified live on claude 2.1.241); Bash itself must NOT appear in
    # the deny list -- deny beats allow and would kill the scoped grants.
    # Verified 2026-08-24 (headless probe): scoped git commands run; any
    # non-whitelisted command (echo ...) is denied in default permission
    # mode (headless cannot grant); output redirection is blocked by the
    # engine even under an allowed prefix ("git log > file" -> "Output
    # redirection ... was blocked", no file created) -- no file-write bypass
    # through the scoped shell.
    # Residual, judged acceptable: git branch/tag have mutating forms
    # (branch/tag creation) a prefix rule cannot split off. They are
    # local-only and reversible, commit/push are NOT whitelisted, and the
    # backstop prompt forbids tagging outright.
    # MultiEdit was dropped from the deny list: unknown tool on this CLI
    # ('Permission deny rule "MultiEdit" matches no known tool' warned on
    # every run's first log line).
    tool_args=(--allowedTools "Read,Grep,Glob,Bash(git log:*),Bash(git branch:*),Bash(git status:*),Bash(git show:*),Bash(git diff:*),Bash(git ls-files:*),Bash(git rev-parse:*),Bash(git merge-base:*),Bash(git tag:*),Bash(git worktree list:*)" \
      --disallowedTools "Task,Agent,Write,Edit,NotebookEdit")
  fi

  # -u CLAUDECODE: nested claude -p is rejected inside a session env.
  # -u PROJECT_ROOT: child usage-gate misclassification guard (spec step 5).
  local claude_rc=0
  run_claude_with_deadline "$(( TIMEOUT_MIN * 60 ))" \
    env -u CLAUDECODE -u PROJECT_ROOT claude -p "$prompt" \
      --model "$MODEL" \
      --append-system-prompt "$backstop" \
      "${tool_args[@]}" \
    || claude_rc=$?

  trap - TERM

  if [[ $claude_rc -eq 124 ]]; then
    finalize_run timed-out 124
  elif [[ $claude_rc -eq 0 ]]; then
    finalize_run completed 0
  else
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
    spawn)  do_spawn ;;
    cancel) do_cancel ;;
    list)   do_list ;;
    worker) do_worker ;;
  esac
}

# Sourcing guard for tests: DISPATCH_TEST=1 loads functions without executing.
if [[ -z "${DISPATCH_TEST:-}" ]]; then
  main
fi
