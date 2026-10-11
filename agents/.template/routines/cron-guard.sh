#!/bin/bash
# cron-guard.sh -- exit!=0 visibility wrapper for launchd routines.
#
# Usage:
#   cron-guard.sh --label <launchd-label> [--friendly-name <text>] \
#                 [--log <stdout-log>] [--env <dotenv>] \
#                 [--queue <class> [--slots N] [--queue-timeout SEC]] \
#                 -- <cmd> [args...]
#
#   --label           launchd job label (used as dedup key; shown as detail in alerts)
#   --friendly-name   human-readable job name for alert headline (optional).
#                     If omitted, the label's last dot-segment is used as headline.
#   --log             path to the job's stdout/stderr log (last N lines attached to alert)
#                     defaults to <AGENT_ROOT>/.telegram_bot/logs/<basename-label>.stdout.log
#   --env             path to .env for push.sh (defaults to AGENT_ROOT/.telegram_bot/.env)
#   --queue           opt-in machine-global concurrency queue class (DGN-360).
#                     Jobs sharing a class across ALL instance roots on this
#                     machine contend for the same slots. Omitted = no queue,
#                     exact legacy behavior (zero-risk default).
#   --slots           max concurrent jobs in the class (requires --queue).
#                     Class defaults: heavy -> 2; any other class -> 1.
#   --queue-timeout   max seconds to wait for a slot (requires --queue).
#                     Default 3600. Policy is FAIL-OPEN: on timeout, log WARN
#                     and run anyway (availability beats strict serialization).
#   --queue-policy    fail-open | defer (default: defer). Governs ONLY the
#                     crew-transaction lease check below (DGN-1509 §5.3/§5.4
#                     Belt A) -- unrelated to --queue/--slots above.
#                     fail-open: never consult a crew lease, run unconditionally
#                     (explicit escape hatch; byte-identical to pre-DGN-1509
#                     cron-guard.sh for a caller that sets this).
#                     defer (default): if AGENT_ROOT shares a kit DB whose
#                     crew lease is DIRTY/INCOMPLETE right now, skip running
#                     the wrapped command entirely UNLESS this unit declares
#                     DOGANY_KIT_DB_FREE=1 (exemption -- an env var, not a
#                     flag, since it is a per-unit PLIST declaration). The
#                     default is "defer" (not "fail-open") because the
#                     member-root default the spec locks is "assume a
#                     consumer unless it says otherwise" (M1) -- but this is
#                     a RUNTIME no-op today: nothing in this estate ever
#                     writes a crew lease outside a live crew-atomic
#                     transaction (scripts/pack/update_apply.sh), and no such
#                     transaction has ever run here (worklog DGN-1509). See
#                     "LEASE-DEFER" below for the mechanics.
#   --unattended      (DGN-1873) the caller runs while the owner is asleep
#                     (the dawn memory queue). No notice from this run carries
#                     an ask button -- it would expire before anyone can tap
#                     it -- and no notice asks a question: the failure alarm
#                     gives a machine-classified reason line and the next
#                     scheduled try (routines/lib/dawn_alarm.py, dec-252).
#   --                separates wrapper args from the real command
#
# Behaviour:
#   1. Run the wrapped command, capture exit code.
#   2. If exit == 0: silent.
#   3. If exit == 75 (EX_TEMPFAIL) AND a FRESH usage-defer marker exists
#      (2-factor signal, DGN-835): recovery branch instead of an alert --
#      five_hour defer -> reset-aligned one-shot retry via usage-defer-retry.sh;
#      seven_day defer -> owner notification with a manual-retry button
#      (dedup per label/day) + a reset-aligned fresh-button re-notify
#      one-shot (DGN-841 B) + a /usageretry slash line in the body (DGN-841 A).
#      Both windows: when the marker carries last_success_at and the job has
#      not succeeded for >1d+6h, ONE stale-canon alert per label/day fires
#      (MAJOR-C reader -- paired with the notify dedup, never shipped alone).
#      exit 75 WITHOUT a fresh marker stays on the normal failure-alert path
#      (an accidental 75 from unrelated code must not be silenced).
#   3b. If exit == 77 AND the backlog marker was written THIS run (DGN-1582):
#      the job finished and committed, but is behind -- one "behind" notice
#      per label/day under its own dedup key, never the failure alarm.
#      Routing (dec-236, replaces dec-235): operator only, never the owner.
#      Night 1 is a log line; the operator (STEWARD_ENV_FILE) hears once per
#      streak from the 2nd consecutive behind night, when memory.py has also
#      raised the next night's budget. No operator -> log only. A night that
#      exits 0 ends the streak.
#      exit 77 without such a marker stays on the normal failure-alert path.
#   3c. If exit == 78 AND the retry marker was written THIS run (DGN-1873):
#      the job failed but has already scheduled its own automatic retry --
#      operator-only notice (dec-236 route, --no-owner-fallback), one per
#      label/day, never the owner. The job itself exits non-78 when the retry
#      also fails, which lands on the normal failure alarm below.
#      exit 78 without such a marker stays on the normal failure-alert path.
#   4. Any other exit != 0: check dedup file. If already fired today for this
#      label, suppress. Otherwise fire push.sh --text alert and write dedup marker.
#
# Usage-defer protocol (DGN-835): this wrapper exports
#   DOGANY_USAGE_DEFER_MARKER=~/.dogany/usage-defer/<label>.json
# to the wrapped command. A job that detects usage exhaustion mid-run writes
# that marker ({window, reset_at, deferred_at, reason, last_success_at}) and
# exits 75. last_success_at (optional, ISO) = the job's own last successful
# completion; it feeds the stale-canon alert reader (MAJOR-C). The marker is
# "fresh" when its mtime is within 10 minutes -- stale markers are ignored so
# an old defer cannot hijack a later genuine failure.
#
# Dedup store: /tmp/dogany-cron-guard/<label>.<YYYYMMDD>
#   One file per (label, date). Cleaned up by the OS on reboot; no maintenance needed.
#
# Queue lock home: ~/.dogany/cron-queue/<class>/slot-N/  (machine-global).
#   macOS ships no flock(1), so each slot is an atomic-mkdir spinlock with a
#   pidfile. A slot whose owner pid is dead is reclaimed (crash/SIGKILL safe).
#   Waiters poll with sleep + jitter (2-5 s) to avoid thundering-herd rescans.
#
# push.sh location: resolved relative to this script (both live in routines/).
# push.sh --env: instance .env; no hardcoded tokens.
#
# Exit code: this wrapper exits with the same code as the wrapped command
#   so launchd's LastExitStatus is preserved.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Shared i18n helper (config/i18n/<lang>.json lookup, DGN-851). Sourcing also
# loads config/agent.conf (AGENT_LANG); AGENT_ROOT is already set above and
# agentlib preserves it. Vars sourced from agent.conf stay shell-local (not
# exported), so they never leak into the wrapped command's environment.
# FAIL-OPEN: this guard wrapper must never die over a missing helper (it is
# also exercised as a standalone copy in tests) -- absent lib degrades
# i18n_or to its in-code default argument (zero-delta).
if [ -f "$SCRIPT_DIR/lib/agentlib.sh" ]; then
  . "$SCRIPT_DIR/lib/agentlib.sh"
else
  i18n_or() { printf '%s' "$2"; }
fi

# DGN-835: snapshot the FULL original argv before destructive parsing -- the
# usage-defer recovery paths replay this exact invocation (retry one-shot /
# manual replay file), argument-by-argument, quoting-safe.
ORIG_ARGS=("$@")

LABEL=""
FRIENDLY_NAME=""
LOG_PATH=""
ENV_PATH="$AGENT_ROOT/.telegram_bot/.env"
DEDUP_DIR="/tmp/dogany-cron-guard"
QUEUE_CLASS=""
QUEUE_SLOTS=""
QUEUE_TIMEOUT=""
QUEUE_BASE_DIR="$HOME/.dogany/cron-queue"
QUEUE_SLOT_DIR=""
QUEUE_POLICY="defer"
UNATTENDED=0

# ---- parse wrapper args (before --) ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --label)         LABEL="$2"; shift 2 ;;
    --friendly-name) FRIENDLY_NAME="$2"; shift 2 ;;
    --log)           LOG_PATH="$2"; shift 2 ;;
    --env)           ENV_PATH="$2"; shift 2 ;;
    --queue)         QUEUE_CLASS="$2"; shift 2 ;;
    --slots)         QUEUE_SLOTS="$2"; shift 2 ;;
    --queue-timeout) QUEUE_TIMEOUT="$2"; shift 2 ;;
    --queue-policy)  QUEUE_POLICY="$2"; shift 2 ;;
    --unattended)    UNATTENDED=1; shift ;;
    --)              shift; break ;;
    *) echo "[cron-guard] unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$LABEL" ]]; then
  echo "[cron-guard] --label is required" >&2; exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "[cron-guard] no command given after --" >&2; exit 1
fi

case "$QUEUE_POLICY" in
  fail-open|defer) : ;;
  *) echo "[cron-guard] invalid --queue-policy (fail-open|defer): $QUEUE_POLICY" >&2; exit 1 ;;
esac

# file_mtime <path>: epoch mtime; GNU (stat -c) first, macOS (stat -f)
# fallback -- GNU `stat -f` is filesystem mode and prints a status block to
# stdout before failing, BSD stat rejects -c (DGN-1920 U1). Defined HERE (moved up from its original spot below the queue
# block, DGN-1509 SPEC v5 §5 carry-over item 3) because the persistent-defer
# alarm below needs it and bash requires a function's definition to have
# already executed before the first call -- inlining a second stat call
# would have duplicated the macOS/GNU fallback dance instead.
file_mtime() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null
}

# ---- operator-alarm display names (DGN-1747) -- existing seats only ----
# _agent_display: the owner-facing agent name from the one resolver
# (routines/lib/agent_name.py, DGN-1744; never fails, folder-name fallback).
_agent_display() {
  python3 "$SCRIPT_DIR/lib/agent_name.py" "$AGENT_ROOT" 2>/dev/null || basename "$AGENT_ROOT"
}

# _job_display <label>: resolve the plist's machine key through the one
# scheduled-job vocabulary seat.  Unknown jobs deliberately remain generic;
# their slug belongs in the folded technical detail, never the headline.
_job_display() {
  python3 "$SCRIPT_DIR/lib/scheduled_job_names.py" "$FRIENDLY_NAME" "$1" \
    2>/dev/null || printf '%s' '예약'
}

# _crow_kit_display <kit>: the crew's owner-facing name through the /update
# display seat (scripts/pack/update_surface.py unit_display: product_name
# over the kit manifest name_ko at the installed tag) of the repo this
# instance updates from; the kit id when that seat is unreachable (the same
# slug fallback unit_display itself applies).
_crow_kit_display() {
  # Two lines: the tagged /update name ("[킷] 라이프킷", the alarm body) and
  # the bare name ("라이프킷", the button -- dec-222: the tapped text becomes
  # the owner's own chat turn, so no layer tag). The slug on both when the
  # seat is unreadable.
  local v
  v="$(python3 - "$AGENT_ROOT" "$1" "${AGENT_LANG:-en}" <<'PY_EOF' 2>/dev/null
import importlib.util, os, sys
root, kit, lang = sys.argv[1], sys.argv[2], sys.argv[3]
def conf(path, key):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith(key + "="):
                    return line[len(key) + 1:].strip().strip("\"'")
    except OSError:
        pass
    return ""
repo = conf(os.path.join(root, ".instance.conf"), "DOGANY_REPO_ROOT")
surface = os.path.join(repo, "scripts", "pack", "update_surface.py")
source = conf(os.path.join(root, "config", "packs", kit + ".receipt"), "source")
tag = source[len("tag:"):] if source.startswith("tag:") else ""
spec = importlib.util.spec_from_file_location("dogany_update_surface", surface)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
tagged = mod.unit_display("kit", kit, tag, [], lang)["product"]
bare = mod.unit_display("kit", kit, tag, [], lang, tagged=False)["product"]
sys.stdout.write("%s\n%s" % (tagged.replace("\n", " "), bare.replace("\n", " ")))
PY_EOF
)"
  if [[ "$v" == *$'\n'* ]]; then
    printf '%s' "$v"
  else
    printf '%s\n%s' "$1" "$1"
  fi
}

# _crow_fill <template> <name> <value> [<name> <value> ...] -- every {name}
# in the i18n template replaced by its value, in one left-to-right pass:
# literal (no pattern or & expansion, same on bash 3.2 and 5.x), and a
# value that itself contains "{name}" is never filled again.
_crow_fill() {
  local rest="$1" out="" head i hit
  shift
  local pairs=("$@")
  while [[ "$rest" == *"{"* ]]; do
    head="${rest%%"{"*}"
    out="$out$head"
    rest="${rest#"$head"}"
    hit=""
    i=0
    while [[ $((i + 1)) -lt ${#pairs[@]} ]]; do
      if [[ "$rest" == "{${pairs[$i]}}"* ]]; then
        out="$out${pairs[$((i + 1))]}"
        rest="${rest#"{${pairs[$i]}}"}"
        hit=1
        break
      fi
      i=$((i + 2))
    done
    if [[ -z "$hit" ]]; then
      out="$out{"
      rest="${rest#"{"}"
    fi
  done
  printf '%s' "$out$rest"
}

# ---- DGN-1509 SPEC v6 §1/§2 -- persistent-defer alarm (S4b, §5.8(2)) ----
# _crow_alarm_key <txn> <kit> <state_file> -- prints the dedup key on
# stdout. Key normalization (v6 §1.2(a)): a bare "alarm-<txn>" key is NOT
# injective on tag alphabet alone -- every reachable txn (a git tag pinned
# through update_plan.sh's pack/<pid>/v* namespace gate, :243/:755-757) has
# EXACTLY two '/' separators, so trading them for '_' is injective on that
# reachable set (v6 §1.2(b) proof; the wall protecting this is the semver
# STRICT precedence lane -- see semver_range.py's cmp_precedence comment).
# Row B/D (txn unidentified, e.g. the C-2 dead-holder state): key falls back
# to kit + the state file's own mtime, the only stable value available
# without a txn (SPEC v5 §1.3).
_crow_alarm_key() {
  local txn="$1" kit="$2" state_f="$3" mt
  if [[ -n "$txn" ]]; then
    printf 'alarm-%s' "$(printf '%s' "$txn" | tr '/' '_')"
  else
    mt="$(file_mtime "$state_f" 2>/dev/null)"
    printf 'alarm-kit-%s-%s' "$kit" "${mt:-0}"
  fi
}

# _crow_alarm_fire <marker_dir> <key> <push_fn> <alert_text> -- SPEC v5 §2.2
# R-1..R-5 marker protocol + v6 §1.2(c) R-2g failure-class guard. Returns 0
# always (fail-open outer contract, same as every other alert path in this
# file); the marker directory itself records what happened for a human to
# read. push_fn is called as `push_fn <text>` -- once with alert_text on the
# main delivery path (R-3), once with a namespace-failure text of this
# function's own composing on either loud guard branch (R-1c / R-2g) --
# never both, so push_fn always receives exactly one text argument.
_crow_alarm_fire() {
  local mdir="$1" key="$2" push_fn="$3" alert_text="$4"
  local kdir="$mdir/$key" age retry="${DOGANY_LEASE_ALARM_RETRY_SEC:-600}"
  local mt now

  # R-1's precondition is "K DIRECTORY exists" (spec v5 §2.2), not merely
  # "something exists at K" -- a non-directory occupant (e.g. a plain file)
  # is namespace interference, not a marker, and must fall straight through
  # to R-2's mkdir so R-2g below can classify and fire loud on it. Checking
  # -e here instead of -d would swallow that case into R-1b's silent-yield
  # branch (a file has an mtime too) and never reach R-2g at all.
  if [[ -d "$kdir" ]]; then
    if [[ -f "$kdir/sent" ]]; then
      return 0  # R-1a: already delivered -- terminal, silent
    fi
    mt="$(file_mtime "$kdir" 2>/dev/null)"; now="$(date +%s)"
    age=$(( now - ${mt:-now} ))
    if [[ "$age" -le "$retry" ]]; then
      return 0  # R-1b: a preemptor is fresh (mid-push or just crashed) -- yield
    fi
    # R-1c: stale preemption -- reclaim via rmdir (only succeeds if empty;
    # a sent-bearing dir is structurally immune, spec v5 §2.2).
    if ! rmdir "$kdir" 2>/dev/null; then
      if [[ -e "$kdir" ]]; then
        if [[ -f "$kdir/sent" ]]; then
          return 0  # sent landed between our checks -- terminal, silent
        fi
        # v6-recheck §5 fix: ENOTEMPTY with sent ABSENT is junk-directory
        # interference (measured: 3 runs, push 0, permanent silence before
        # this guard) -- route to the loud lane instead of reading it as a
        # legitimate in-flight preemption.
        "$push_fn" "[cron-guard] LEASE-ALARM 이름공간 실패: $kdir 가 비어있지 않은데 sent 마커가 없어요 (간섭 의심 -- 손으로 확인해주세요, DGN-1509 R-1c)."
        return 0
      fi
      # else: ENOENT -- a racing reclaimer already removed it; fall through
      # to R-2 below and try to become the new preemptor.
    fi
  fi

  # R-2: preempt. Plain mkdir (NEVER -p, v6 §1.2(d)) -- -p returns rc=0 on
  # an EXISTING directory too, so a losing racer could not tell it lost
  # (measured regression: 8-way race -> push 8 with -p vs push 1 without).
  if mkdir "$kdir" 2>/dev/null; then
    if "$push_fn" "$alert_text"; then
      touch "$kdir/sent"  # R-5: delivered, terminal
    else
      rmdir "$kdir" 2>/dev/null || true  # R-4: rc-failure, give the marker back
    fi
  elif [[ ! -d "$kdir" ]]; then
    # R-2g (v6 §1.2(c)): mkdir failed AND it is not a directory -- this is
    # NOT the ordinary "lost the preemption race" case (that always leaves
    # a directory behind); it is namespace interference (e.g. a plain file
    # sitting on K). Silence is never the right read for that -- fire loud.
    "$push_fn" "[cron-guard] LEASE-ALARM 이름공간 실패: $kdir mkdir 실패 (디렉터리 아님 -- 간섭 의심, DGN-1509 R-2g)."
  fi
  # else: mkdir failed and $kdir IS a directory -- legitimate preemption
  # loss (R-2 plain case); the winner fires, this run stays silent.
  return 0
}

# ---- LEASE-DEFER (DGN-1509 §5.3 Belt A / §5.4 M1) ----------------------
# Runs BEFORE the queue-slot wait and BEFORE the wrapped command -- a unit
# that is about to be deferred should never wait on a slot or execute at
# all. Read-only: this wrapper never writes a lease, only reads one (the
# sole producer is scripts/pack/update_apply.sh's crew-atomic transaction).
#
# Member-root determination: does ANY kit this instance shares
# (AGENT_ROOT/database/*.db resolving under $SHARED_HOME/crews/<kit>/)
# currently have a DIRTY/INCOMPLETE lease? "any", not "the one this unit is
# declared to use" -- spec §5.4's fail-closed default is "assume a consumer
# unless it opts out" (M1: the generic-brief/domain-section.sh counter-
# example is a payload-shipped hook that opens a kit DB with NO trace in the
# plist/env a per-kit allowlist could read; scanning per-unit declarations
# would miss exactly that shape).
#
# Exemption: DOGANY_KIT_DB_FREE=1 in THIS process's environment (a plist
# EnvironmentVariables entry, inherited by cron-guard.sh itself since
# launchd sets it for the whole ProgramArguments invocation -- readable here
# BEFORE running the wrapped command, which a runtime-only declaration
# could not be).
if [[ "$QUEUE_POLICY" == "defer" && "${DOGANY_KIT_DB_FREE:-0}" != "1" ]]; then
  # Read-only, inlined (not sourced from scripts/pack/lib/crew_lease.sh):
  # that lib lives in the framework REPO tree (install/update-time tooling)
  # and is never copied into a live instance's routines/ tree -- this
  # wrapper runs from the INSTANCE side, a different filesystem home
  # entirely. The read this needs is one file's content, so it is inlined
  # rather than shipping a second copy of the primitive to keep in sync
  # (the WRITE side stays exclusively in scripts/pack/lib/crew_lease.sh --
  # this wrapper never creates or clears a lease, only reads state).
  _CROW_SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
  _CROW_CREW_DIR="${DOGANY_CREW_DIR:-$_CROW_SHARED_HOME/crews}"
  _CROW_DEFER_KIT=""
  if [[ -d "$AGENT_ROOT/database" ]]; then
    for _crow_db in "$AGENT_ROOT"/database/*.db; do
      [[ -L "$_crow_db" ]] || continue
      _crow_kit="$(basename "$_crow_db" .db)"
      _crow_target="$(readlink "$_crow_db" 2>/dev/null || true)"
      [[ "$_crow_target" == "$_CROW_SHARED_HOME/crews/$_crow_kit/$_crow_kit.db" ]] || continue
      _crow_state_f="$_CROW_CREW_DIR/$_crow_kit/.lease/state"
      [[ -f "$_crow_state_f" ]] || continue
      case "$(tr -d '[:space:]' < "$_crow_state_f" 2>/dev/null)" in
        DIRTY|INCOMPLETE) _CROW_DEFER_KIT="$_crow_kit"; break ;;
      esac
    done
  fi
  if [[ -n "$_CROW_DEFER_KIT" ]]; then
    # lease-defer marker (DGN-1509 M2): its own directory, its own exit
    # code (76, NOT DGN-835's 75 -- cron-guard.sh:371-377's 2-factor for 75
    # is usage-defer's alone; reusing it would make lease-defer's silence
    # ALSO silence an unrelated accidental-75 from the wrapped command, the
    # exact thing DGN-835 refuses). 76 is checked WITH a fresh marker below
    # for the same reason DGN-835 checks 75 with one: a bare 76 from some
    # unrelated wrapped command must stay on the normal failure-alert path.
    _CROW_MARKER_DIR="$_CROW_SHARED_HOME/lease-defer"
    mkdir -p "$_CROW_MARKER_DIR" 2>/dev/null || true
    _CROW_MARKER="$_CROW_MARKER_DIR/${LABEL}.json"
    printf '{"kit":"%s","label":"%s","deferred_at":"%s"}\n' \
      "$_CROW_DEFER_KIT" "$LABEL" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$_CROW_MARKER" 2>/dev/null || true
    # M6 (DGN-1509 S4 landing condition): lease-holding visibility (age /
    # holder / txn) rides the SAME terminal-state ledger beat this file
    # already writes unconditionally on every run (below, line ~410) --
    # this early one is the deferred-run's OWN beat (the code past this
    # `exit 76` never runs, so without this call a deferred label would show
    # NO beat at all, reading as "never ran" rather than "ran, deferred").
    # Without this, S3's lease primitive existing would silently make S4's
    # "생산자 없음 = 무변화" claim false the moment a lease is ever held with
    # no surface saying so (grill M6's exact worry).
    _CROW_HOLDER_F="$_CROW_CREW_DIR/$_CROW_DEFER_KIT/.lease/holder"
    _CROW_NOTE="lease-defer kit=$_CROW_DEFER_KIT"
    _CROW_TXN=""; _CROW_STARTED=""; _CROW_HOLDER_PID=""; _CROW_AGE=""
    if [[ -f "$_CROW_HOLDER_F" ]]; then
      _CROW_TXN="$(sed -n 's/^txn=//p' "$_CROW_HOLDER_F" | head -1)"
      _CROW_STARTED="$(sed -n 's/^started_at=//p' "$_CROW_HOLDER_F" | head -1)"
      _CROW_HOLDER_PID="$(sed -n 's/^pid=//p' "$_CROW_HOLDER_F" | head -1)"
      if [[ "$_CROW_STARTED" =~ ^[0-9]+$ ]]; then
        _CROW_AGE="$(( $(date +%s) - _CROW_STARTED ))s"
      fi
      _CROW_NOTE="lease-defer kit=$_CROW_DEFER_KIT holder=${_CROW_HOLDER_PID:-?} txn=${_CROW_TXN:-?} age=${_CROW_AGE:-?}"
    fi
    if [[ -f "$SCRIPT_DIR/terminal-state-ledger.py" ]]; then
      python3 "$SCRIPT_DIR/terminal-state-ledger.py" beat --surface cron \
        --id "$LABEL" --rc 76 --note "$_CROW_NOTE" \
        ${DOGANY_TSL_BEAT_TTL:+--ttl "$DOGANY_TSL_BEAT_TTL"} >/dev/null 2>&1 || true
    fi

    # Persistent-defer alarm (DGN-1509 SPEC v6, §5.8(2)/criterion 17): fires
    # from THIS branch (criterion 17(i) -- must fire even with the sweep
    # below disabled/dead, never routed through it). age known (holder
    # present, started_at numeric, SPEC v5 §1.3 rows A/B): only once past
    # the threshold -- an in-progress transaction younger than that is
    # healthy, not an alarm. age UNKNOWN (rows C/D -- holder missing/
    # corrupt, e.g. the crash-during-clear C-2 state crew_lease.sh:38-41
    # documents as never auto-reclaimed): no reachable healthy state
    # produces this, so it fires immediately, ignoring the threshold.
    _CROW_ALARM_AGE_NUM=""
    if [[ "$_CROW_STARTED" =~ ^[0-9]+$ ]]; then
      _CROW_ALARM_AGE_NUM=$(( $(date +%s) - _CROW_STARTED ))
    fi
    _CROW_ALARM_THRESHOLD="${DOGANY_LEASE_ALARM_THRESHOLD_SEC:-1800}"
    if [[ -z "$_CROW_ALARM_AGE_NUM" || "$_CROW_ALARM_AGE_NUM" -gt "$_CROW_ALARM_THRESHOLD" ]]; then
      _crow_alarm_push() {
        local text="$1" ps="$SCRIPT_DIR/push.sh"
        # The ask button rides only the main alarm, never the loud
        # namespace-failure lines _crow_alarm_fire may push instead.
        local btn=()
        # DGN-1873: never on an unattended (dawn) run -- it would expire.
        [[ "$text" == "$_CROW_ALARM_TEXT" && -n "$_CROW_ALARM_BUTTON" && "$UNATTENDED" -eq 0 ]] && btn=(--button "$_CROW_ALARM_BUTTON")
        if [[ -x "$ps" ]]; then
          if [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]]; then
            "$ps" --text "$text" --env "$ENV_PATH" --audience operator ${btn[@]+"${btn[@]}"} >&2
          else
            "$ps" --text "$text" --audience operator ${btn[@]+"${btn[@]}"} >&2
          fi
        else
          echo "[cron-guard] LEASE-ALARM push.sh 없음: $ps" >&2
          return 1
        fi
      }
      # Owner-approved copy (2026-09-27, DGN-1747): first person -- the
      # agent whose chat this lands in (the steward, else the owner's own
      # agent) speaks, and it ASKS instead of claiming an investigation
      # nobody runs. The crew name comes from the /update display seat; the
      # machine detail (kit, holder, txn, age, ticket) rides only the folded
      # block below the body, never the headline. Age unknown (rows C/D):
      # the minutes count from the lease state file's own mtime, the moment
      # the transaction went DIRTY/INCOMPLETE.
      _CROW_KIT_NAMES="$(_crow_kit_display "$_CROW_DEFER_KIT")"
      _CROW_KIT_DISP="${_CROW_KIT_NAMES%%$'\n'*}"
      _CROW_KIT_BARE="${_CROW_KIT_NAMES#*$'\n'}"
      _CROW_ALARM_MIN="$_CROW_ALARM_AGE_NUM"
      if [[ -z "$_CROW_ALARM_MIN" ]]; then
        _crow_state_mt="$(file_mtime "$_crow_state_f" 2>/dev/null || true)"
        [[ "$_crow_state_mt" =~ ^[0-9]+$ ]] && _CROW_ALARM_MIN=$(( $(date +%s) - _crow_state_mt ))
      fi
      _CROW_ALARM_MIN=$(( ${_CROW_ALARM_MIN:-0} / 60 ))
      [[ "$_CROW_ALARM_MIN" -lt 1 ]] && _CROW_ALARM_MIN=1
      # DGN-1790 r2: body, fold and button are i18n (cron.lease_alarm_*);
      # the in-code defaults are English.
      _CROW_ALARM_BODY="$(_crow_fill "$(i18n_or cron.lease_alarm_body "⚠️ The {kit} update has not finished for {min} min.
Shall I check why?")" kit "$_CROW_KIT_DISP" min "$_CROW_ALARM_MIN")"
      if [[ "$UNATTENDED" -eq 1 ]]; then
        # DGN-1873 Q2: an unattended (dawn) run never asks -- the button
        # (dropped in _crow_alarm_push) would expire before morning. The body
        # says this run was skipped and when it is tried again: the job's
        # next scheduled fire, the same source as the dawn failure alarm
        # (routines/lib/dawn_alarm.py).
        # Owner-approved wording (dec-255, 2026-10-06, DGN-1873 Q2). ko words
        # in config/i18n.
        _CROW_ALARM_WHEN="$(python3 "$SCRIPT_DIR/lib/dawn_alarm.py" when --label "$LABEL" \
          --lang "${AGENT_LANG:-en}" 2>/dev/null)"
        _CROW_ALARM_BODY="$(_crow_fill "$(i18n_or cron.lease_alarm_body_unattended "⚠️ The {kit} update has not finished for {min} min.
So this scheduled run was skipped.
I'll try again {when}.")" kit "$_CROW_KIT_DISP" min "$_CROW_ALARM_MIN" \
          job "$(_job_display "$LABEL")" when "${_CROW_ALARM_WHEN:-at the next scheduled run}")"
      fi
      _CROW_ALARM_TEXT="${_CROW_ALARM_BODY}
$(_crow_fill "$(i18n_or cron.lease_alarm_detail ">! Technical details
> Kit: {kit_id}
> Lock holder pid: {pid} / txn: {txn} / age: {age}
> Ref: DGN-1509")" kit_id "$_CROW_DEFER_KIT" pid "${_CROW_HOLDER_PID:-?}" txn "${_CROW_TXN:-?}" age "${_CROW_AGE:-?}")"
      # The tapped button's text becomes the session turn (bot.py opt:
      # handler -> resolve_choice), so it names the crew it is about --
      # by its bare name, no layer tag (dec-222).
      _CROW_ALARM_BUTTON="$(_crow_fill "$(i18n_or cron.lease_alarm_button "Check why the {kit} update is delayed")" kit "$_CROW_KIT_BARE")::opt:1"
      _CROW_ALARM_KEY="$(_crow_alarm_key "$_CROW_TXN" "$_CROW_DEFER_KIT" "$_crow_state_f")"
      _crow_alarm_fire "$_CROW_MARKER_DIR" "$_CROW_ALARM_KEY" _crow_alarm_push "$_CROW_ALARM_TEXT"
    fi

    echo "[cron-guard] LEASE-DEFER: crew '$_CROW_DEFER_KIT' lease is DIRTY/INCOMPLETE -- skipping this run without executing it (marker: $_CROW_MARKER, DGN-1509). Declare DOGANY_KIT_DB_FREE=1 in this unit's plist if it never opens a crew-shared kit DB." >&2
    exit 76
  fi
fi

# ---- machine-global cron queue (opt-in; DGN-360) ----
# No --queue given: the whole block below is skipped and the wrapper behaves
# exactly like the pre-queue version (no traps, no extra filesystem access).

# release_queue_slot: free the held slot (rm the lock dir). No-op if none held.
release_queue_slot() {
  if [[ -n "$QUEUE_SLOT_DIR" ]]; then
    rm -rf "$QUEUE_SLOT_DIR" 2>/dev/null || true
    QUEUE_SLOT_DIR=""
  fi
}

# reclaim_queue_slot <slot_dir>: remove a stale slot via atomic rename so two
# racing waiters cannot both delete it (only one mv succeeds; the loser sees
# ENOENT and simply retries mkdir). Returns 0 if this process did the reclaim.
reclaim_queue_slot() {
  local slot_dir="$1" trash
  trash="$QUEUE_CLASS_DIR/.reclaim.$$.$RANDOM"
  if mv "$slot_dir" "$trash" 2>/dev/null; then
    rm -rf "$trash" 2>/dev/null || true
    return 0
  fi
  return 1
}

# try_queue_slot <slot_dir>: attempt to acquire one slot.
#   0 = acquired (QUEUE_SLOT_DIR set), 1 = busy.
try_queue_slot() {
  local slot_dir="$1" owner_pid owner_pid2 dir_mtime now
  if mkdir "$slot_dir" 2>/dev/null; then
    QUEUE_SLOT_DIR="$slot_dir"
    echo "$$" > "$slot_dir/pid"
    return 0
  fi

  # Slot occupied: stale-owner detection.
  owner_pid="$(cat "$slot_dir/pid" 2>/dev/null || true)"
  if [[ -n "$owner_pid" ]]; then
    if kill -0 "$owner_pid" 2>/dev/null; then
      return 1  # live owner
    fi
    # Owner pid is dead. Re-read the pidfile just before reclaiming to shrink
    # the window where a fresh acquirer replaced the slot under our feet.
    owner_pid2="$(cat "$slot_dir/pid" 2>/dev/null || true)"
    if [[ "$owner_pid2" == "$owner_pid" ]] && reclaim_queue_slot "$slot_dir"; then
      echo "[cron-guard] reclaimed stale queue slot (dead pid $owner_pid): $slot_dir" >&2
      if mkdir "$slot_dir" 2>/dev/null; then
        QUEUE_SLOT_DIR="$slot_dir"
        echo "$$" > "$slot_dir/pid"
        return 0
      fi
    fi
    return 1
  fi

  # No pidfile: owner may be between mkdir and pidfile write. Only treat as
  # stale (crash in that gap) if the dir is older than 60 s.
  dir_mtime="$(file_mtime "$slot_dir")"
  now="$(date +%s)"
  if [[ -n "$dir_mtime" ]] && [[ $(( now - dir_mtime )) -gt 60 ]]; then
    if reclaim_queue_slot "$slot_dir"; then
      echo "[cron-guard] reclaimed abandoned queue slot (no pidfile, age>60s): $slot_dir" >&2
      if mkdir "$slot_dir" 2>/dev/null; then
        QUEUE_SLOT_DIR="$slot_dir"
        echo "$$" > "$slot_dir/pid"
        return 0
      fi
    fi
  fi
  return 1
}

# acquire_queue_slot: scan slot-1..slot-N until acquired or deadline passes.
#   0 = acquired, 1 = timed out (caller applies fail-open policy).
acquire_queue_slot() {
  local deadline i notice=0
  deadline=$(( $(date +%s) + QUEUE_TIMEOUT ))
  while :; do
    i=1
    while [[ $i -le $QUEUE_SLOTS ]]; do
      if try_queue_slot "$QUEUE_CLASS_DIR/slot-$i"; then
        return 0
      fi
      i=$((i + 1))
    done
    if [[ "$(date +%s)" -ge "$deadline" ]]; then
      return 1
    fi
    if [[ "$notice" -eq 0 ]]; then
      echo "[cron-guard] queue wait: class=$QUEUE_CLASS slots=$QUEUE_SLOTS label=$LABEL" >&2
      notice=1
    fi
    # poll with jitter: 2-5 s between scans
    sleep $(( 2 + RANDOM % 4 ))
  done
}

if [[ -n "$QUEUE_CLASS" ]]; then
  # class name becomes a single path segment -- restrict charset
  case "$QUEUE_CLASS" in
    *[!A-Za-z0-9._-]*)
      echo "[cron-guard] invalid --queue class (allowed: A-Za-z0-9._-): $QUEUE_CLASS" >&2
      exit 1 ;;
  esac

  # class defaults: heavy -> slots=2; any other class -> slots=1; timeout 3600
  if [[ -z "$QUEUE_SLOTS" ]]; then
    case "$QUEUE_CLASS" in
      heavy) QUEUE_SLOTS=2 ;;
      *)     QUEUE_SLOTS=1 ;;
    esac
  fi
  if [[ -z "$QUEUE_TIMEOUT" ]]; then
    QUEUE_TIMEOUT=3600
  fi

  case "$QUEUE_SLOTS" in
    ''|0|*[!0-9]*) echo "[cron-guard] invalid --slots (positive integer): $QUEUE_SLOTS" >&2; exit 1 ;;
  esac
  case "$QUEUE_TIMEOUT" in
    ''|*[!0-9]*) echo "[cron-guard] invalid --queue-timeout (seconds): $QUEUE_TIMEOUT" >&2; exit 1 ;;
  esac

  QUEUE_CLASS_DIR="$QUEUE_BASE_DIR/$QUEUE_CLASS"
  mkdir -p "$QUEUE_CLASS_DIR"

  # Free the slot on ANY exit. Signal traps route through exit so the EXIT
  # trap fires on TERM/INT/HUP mid-run too. SIGKILL / hard crash leaves the
  # slot behind -- covered by dead-pid stale reclaim above.
  trap release_queue_slot EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM

  QUEUE_WAIT_START="$(date +%s)"
  if ! acquire_queue_slot; then
    # FAIL-OPEN: availability of backups/consolidation beats serialization.
    echo "[cron-guard] WARN: queue timeout after $(( $(date +%s) - QUEUE_WAIT_START ))s (class=$QUEUE_CLASS slots=$QUEUE_SLOTS timeout=${QUEUE_TIMEOUT}s label=$LABEL) -- running anyway (fail-open)" >&2
  fi
elif [[ -n "$QUEUE_SLOTS" || -n "$QUEUE_TIMEOUT" ]]; then
  echo "[cron-guard] --slots/--queue-timeout require --queue <class>" >&2
  exit 1
fi

# ---- default log path ----
# derive from label: take the last segment after the final dot
if [[ -z "$LOG_PATH" ]]; then
  LOG_BASENAME="${LABEL##*.}"
  LOG_PATH="$AGENT_ROOT/.telegram_bot/logs/${LOG_BASENAME}.stdout.log"
fi

# ---- run the wrapped command ----
# DGN-835 usage-defer protocol: hand the wrapped command its per-label marker
# path. A usage-aware job writes this marker and exits 75 to request recovery.
USAGE_DEFER_DIR="$HOME/.dogany/usage-defer"
USAGE_DEFER_MARKER="$USAGE_DEFER_DIR/${LABEL}.json"
mkdir -p "$USAGE_DEFER_DIR" 2>/dev/null || true
export DOGANY_USAGE_DEFER_MARKER="$USAGE_DEFER_MARKER"

# DGN-1582 (1) behind protocol: hand the wrapped command its per-label
# backlog marker path. A job whose run finished but left a backlog that has
# outlived its drain allowance writes this marker and exits 77 (see the
# behind branch below). Same 2-factor shape as the usage-defer marker above.
BACKLOG_DIR="$HOME/.dogany/backlog-behind"
BACKLOG_MARKER="$BACKLOG_DIR/${LABEL}.json"
mkdir -p "$BACKLOG_DIR" 2>/dev/null || true
export DOGANY_BACKLOG_MARKER="$BACKLOG_MARKER"

# DGN-1873 retry-scheduled protocol: a job that failed but has its own
# automatic retry already scheduled writes this marker and exits 78 (see the
# retry branch below). Same 2-factor shape as the markers above.
RETRY_DIR="$HOME/.dogany/retry-scheduled"
RETRY_MARKER="$RETRY_DIR/${LABEL}.json"
mkdir -p "$RETRY_DIR" 2>/dev/null || true
export DOGANY_RETRY_MARKER="$RETRY_MARKER"

# DGN-1582 (2): run-start epoch -- a log older than this holds no output of THIS run.
RUN_START_EPOCH="$(date +%s)"
# DGN-1873: the logs' sizes before the run -- the bytes past these offsets are
# THIS run's own output (a shared per-day log, e.g. the dawn queue's instance
# log, also holds earlier steps). The unattended alarm classifies its reason
# line from them.
STDERR_LOG_PATH="${LOG_PATH%.stdout.log}.stderr.log"
file_size() {
  stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null || echo 0
}
LOG_OFFSET="$(file_size "$LOG_PATH")"
STDERR_LOG_OFFSET="$(file_size "$STDERR_LOG_PATH")"
EXIT_CODE=0
"$@" || EXIT_CODE=$?

# ---- DGN-1012: cron liveness beat ----
# One ledger row per wrapped run makes "the job did not run" (no fresh beat)
# distinguishable from "the job ran clean" (beat rc=0) -- the two were
# indistinguishable before (DGN-1012 surface 3). Record-only by default;
# a plist that declares its cadence via DOGANY_TSL_BEAT_TTL (seconds) arms a
# missed-next-beat expiry in the sweep. rc!=0 does NOT alert here -- the
# failure-alert below already owns that (no duplicate nag). Fail-open.
if [[ -f "$SCRIPT_DIR/terminal-state-ledger.py" ]]; then
  # (stderr NOT swallowed: a failing beat leaves a trace in the job's stderr
  # log instead of dying silently -- self-grill finding, L5. The existence
  # guard keeps standalone test copies of this wrapper zero-delta.)
  python3 "$SCRIPT_DIR/terminal-state-ledger.py" beat --surface cron \
    --id "$LABEL" --rc "$EXIT_CODE" \
    ${DOGANY_TSL_BEAT_TTL:+--ttl "$DOGANY_TSL_BEAT_TTL"} >/dev/null || true
fi

# DGN-1683: the mint worker is detached, so a later periodic job is its
# recovery drive if it dies between owner turns. This is intentionally
# isolated from the wrapped job's exit code.
MINT_FLOW_SWEEP="$AGENT_ROOT/scripts/pack/mint_flow_sweep.sh"
if [[ -x "$MINT_FLOW_SWEEP" ]]; then
  "$MINT_FLOW_SWEEP" >/dev/null 2>&1 || true
fi

# ---- DGN-1655: no sweep leg here any more ----
# This wrapper used to run `terminal-state-ledger.py sweep --throttle` after
# EVERY wrapped job (DGN-1012 M2 leg 1). That convenience was the defect:
# whichever job ran often enough became the drive, and a feature timer
# (mirror-poll) masked four instances that had no driver of their own. The
# sweep is now driven ONLY by the framework-owned unit
# com.telegram-skill-bot.<agent>.instance-drive, which runs THROUGH this
# wrapper; its liveness row is reserved by the ledger engine (F2), so no
# wrapped job can make it, and the cron beat above (id=<label>) is the unit's
# own "did it fire" row that the ledger checker reads next to the sweep row
# (F7).
#
# The drive's failure keeps the owner-approved sweep alert (DGN-1747) under
# its OWN dedup key -- never a feature job's name -- and stands in for the
# generic failure alarm below (one failure, one message). Exit code is the
# drive's own; nothing here can paint another job red.
TSL_DRIVE_SUFFIX=".instance-drive"
if [[ "$LABEL" == *"$TSL_DRIVE_SUFFIX" && "$EXIT_CODE" -ne 0 ]]; then
  TSL_SWEEP_RC="$EXIT_CODE"
  TSL_ALERT_MARK="$DEDUP_DIR/terminal-state-sweep.$(date +%Y%m%d)"
  if [[ -f "$TSL_ALERT_MARK" ]]; then
    echo "[cron-guard] terminal-state sweep failed rc=$TSL_SWEEP_RC (owner alert already sent today)" >&2
  else
    mkdir -p "$DEDUP_DIR" 2>/dev/null || true
    touch "$TSL_ALERT_MARK"       # mark BEFORE pushing (dup guard on crash)
    TSL_PUSH_SH="$SCRIPT_DIR/push.sh"
    # Owner-approved copy (2026-09-27, DGN-1747), verbatim. The "checks
    # again within an hour" line is true by construction: the drive unit
    # fires every TTL/4 (terminal-state-ledger.py policy; 1800s today).
    # The return code rides only the folded detail block.
    TSL_ALERT_TEXT="⚠️ 끝나지 않은 작업을 찾는 점검이 실패했어요.
놓친 작업이 있을 수 있어요. 한 시간 안에 다시 점검해요.
>! 기술 상세
> 종료코드: ${TSL_SWEEP_RC}"
    echo "[cron-guard] terminal-state sweep notify failed rc=$TSL_SWEEP_RC (owner alerted in plain language)" >&2
    if [[ -x "$TSL_PUSH_SH" ]]; then
      if [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]]; then
        "$TSL_PUSH_SH" --text "$TSL_ALERT_TEXT" --env "$ENV_PATH" --audience operator >&2 || true
      else
        "$TSL_PUSH_SH" --text "$TSL_ALERT_TEXT" --audience operator >&2 || true
      fi
    else
      echo "[cron-guard] terminal-state sweep failed rc=$TSL_SWEEP_RC and push.sh is unavailable: $TSL_PUSH_SH" >&2
    fi
  fi
  exit "$EXIT_CODE"
fi

# ---- success path: silent ----
# DGN-1582 (dec-235): a clean night ends the behind streak, so the next
# behind run starts counting again from night 1.
if [[ "$EXIT_CODE" -eq 0 ]]; then
  rm -f "$BACKLOG_DIR/${LABEL}.streak" 2>/dev/null || true
  exit 0
fi

# ---- DGN-1582 (1): behind branch (exit 77 + marker written THIS run) ----
# A consolidate night that finished inside its budget and committed its work,
# but whose backlog has not drained for several nights. That is a designed
# outcome with a real signal ("the budget is not keeping up"), not a failed
# job: it gets its own notice and its own dedup key, never the failure alarm
# (which would also stay deduped for the rest of the day). 2-factor like 75:
# a bare 77 from an unrelated command, or a marker older than this run,
# stays on the normal failure path below.
if [[ "$EXIT_CODE" -eq 77 && -f "$BACKLOG_MARKER" ]]; then
  BEHIND_MTIME="$(file_mtime "$BACKLOG_MARKER")"
  if [[ -n "$BEHIND_MTIME" && "$BEHIND_MTIME" -ge "$RUN_START_EPOCH" ]]; then
    BEHIND_INFO="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    print("%d|%d|%d" % (int(d.get("runs", 0)), int(d.get("rows_left", 0)),
                        1 if d.get("budget_raised") else 0))
except Exception:
    print("")
' "$BACKLOG_MARKER" 2>/dev/null)"
    IFS='|' read -r BEHIND_RUNS BEHIND_ROWS BEHIND_RAISED <<< "$BEHIND_INFO"
    echo "[cron-guard] BEHIND: $LABEL exit=77 runs=${BEHIND_RUNS:-?} rows_left=${BEHIND_ROWS:-?} (work committed; behind notice, not a failure alarm)" >&2
    BEHIND_DEDUP="$DEDUP_DIR/${LABEL}.behind.$(date +%Y%m%d)"
    mkdir -p "$DEDUP_DIR" 2>/dev/null || true
    if [[ -f "$BEHIND_DEDUP" ]]; then
      echo "[cron-guard] behind notice suppressed (dedup): $LABEL" >&2
      exit "$EXIT_CODE"
    fi
    touch "$BEHIND_DEDUP"
    # dec-235 streak: consecutive behind nights for this label, counted once
    # per day; a night that exits 0 removes the file (success path above).
    # Plain "count date notified" text -- one line, read without python.
    BEHIND_STREAK_FILE="$BACKLOG_DIR/${LABEL}.streak"
    STREAK_N=0; STREAK_DAY=""; STREAK_NOTIFIED=0
    if [[ -f "$BEHIND_STREAK_FILE" ]]; then
      read -r STREAK_N STREAK_DAY STREAK_NOTIFIED < "$BEHIND_STREAK_FILE" || true
      [[ "$STREAK_N" =~ ^[0-9]+$ ]] || STREAK_N=0
      [[ "$STREAK_NOTIFIED" == "1" ]] || STREAK_NOTIFIED=0
    fi
    TODAY_DAY="$(date +%Y%m%d)"
    if [[ "$STREAK_DAY" != "$TODAY_DAY" ]]; then
      STREAK_N=$((STREAK_N + 1))
      STREAK_DAY="$TODAY_DAY"
    fi
    printf '%s %s %s\n' "$STREAK_N" "$STREAK_DAY" "$STREAK_NOTIFIED" > "$BEHIND_STREAK_FILE" 2>/dev/null || true
    # dec-236: behind is operator-only, never the owner (the owner cannot act
    # on it; the program raises the next night's budget itself -- memory.py
    # records that in the marker). Night 1 is a log line only; from the 2nd
    # consecutive behind night the operator hears once per streak.
    if [[ "$STREAK_N" -lt 2 ]]; then
      echo "[cron-guard] behind night $STREAK_N: log only, operator told from night 2 (dec-236)" >&2
      exit "$EXIT_CODE"
    fi
    if [[ "$STREAK_NOTIFIED" == "1" ]]; then
      echo "[cron-guard] behind night $STREAK_N: operator already told this streak (dec-236)" >&2
      exit "$EXIT_CODE"
    fi
    PUSH_SH="$SCRIPT_DIR/push.sh"
    PUSH_ENV_ARGS=()
    [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]] && PUSH_ENV_ARGS=(--env "$ENV_PATH")
    if [[ ! -x "$PUSH_SH" ]]; then
      echo "[cron-guard] push.sh not found or not executable: $PUSH_SH" >&2
      exit "$EXIT_CODE"
    fi
    if [[ "$BEHIND_RAISED" == "1" ]]; then
      # Owner-confirmed copy (dec-236); ko/en words in config/i18n.
      BEHIND_TEXT="$(_crow_fill "$(i18n_or cron.backlog_behind_operator_body "{agent}'s memory cleanup did not finish for the second night in a row. {rows} item(s) are left, so I gave the next night more working time.")" \
        agent "$(_agent_display)" rows "${BEHIND_ROWS:-?}")"
    else
      # Budget already at its cap (nothing was raised): the "more time" copy
      # would be false. DRAFT operator-only wording -- WORDING PENDING OWNER
      # CONFIRMATION (DGN-1582 r1 key, no i18n entry yet).
      BEHIND_TEXT="$(_crow_fill "$(i18n_or cron.backlog_behind_body "{agent}'s {job} did its share tonight but is still behind: {rows} message(s) carried over, {runs} nights in a row.
The nightly time budget is already at its cap.
>! Technical details
> Job label: {label}
> Return code: {rc}")" agent "$(_agent_display)" job "$(_job_display "$LABEL")" \
        runs "$STREAK_N" rows "${BEHIND_ROWS:-?}" label "$LABEL" rc "$EXIT_CODE")"
    fi
    # --no-owner-fallback: push.sh answers rc 9 (nothing sent) when
    # STEWARD_ENV_FILE resolves no operator (DGN-1808 / dec-220 route).
    OP_RC=0
    "$PUSH_SH" --text "$BEHIND_TEXT" ${PUSH_ENV_ARGS[@]+"${PUSH_ENV_ARGS[@]}"} --audience operator --no-owner-fallback >&2 || OP_RC=$?
    if [[ "$OP_RC" -eq 0 || "$OP_RC" -eq 9 ]]; then
      [[ "$OP_RC" -eq 9 ]] && echo "[cron-guard] behind night $STREAK_N: no operator -- log only, the owner is never told (dec-236)" >&2
      printf '%s %s 1\n' "$STREAK_N" "$STREAK_DAY" > "$BEHIND_STREAK_FILE" 2>/dev/null || true
    else
      echo "[cron-guard] operator behind notice not delivered (push rc=$OP_RC); retried next behind night" >&2
    fi
    exit "$EXIT_CODE"
  fi
fi

# ---- DGN-1873: retry-scheduled branch (exit 78 + marker written THIS run) ----
# Owner directive 2026-10-06: a failure the job recovers from by itself must
# not reach the owner. The job (not this wrapper, and never a guess from the
# label) declares that a retry is scheduled; the owner hears only when that
# retry also fails, which the job reports with a plain non-zero exit.
if [[ "$EXIT_CODE" -eq 78 && -f "$RETRY_MARKER" ]]; then
  RETRY_MTIME="$(file_mtime "$RETRY_MARKER")"
  if [[ -n "$RETRY_MTIME" && "$RETRY_MTIME" -ge "$RUN_START_EPOCH" ]]; then
    RETRY_WHAT="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        print(str(json.load(f).get("what", ""))[:80])
except Exception:
    print("")
' "$RETRY_MARKER" 2>/dev/null)"
    echo "[cron-guard] RETRY-SCHEDULED: $LABEL exit=78 what=${RETRY_WHAT:-?} (job retries by itself; operator-only notice, never the owner)" >&2
    RETRY_DEDUP="$DEDUP_DIR/${LABEL}.retry.$(date +%Y%m%d)"
    mkdir -p "$DEDUP_DIR" 2>/dev/null || true
    if [[ -f "$RETRY_DEDUP" ]]; then
      echo "[cron-guard] retry-scheduled notice suppressed (dedup): $LABEL" >&2
      exit "$EXIT_CODE"
    fi
    touch "$RETRY_DEDUP"
    PUSH_SH="$SCRIPT_DIR/push.sh"
    PUSH_ENV_ARGS=()
    [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]] && PUSH_ENV_ARGS=(--env "$ENV_PATH")
    if [[ ! -x "$PUSH_SH" ]]; then
      echo "[cron-guard] push.sh not found or not executable: $PUSH_SH" >&2
      exit "$EXIT_CODE"
    fi
    # DRAFT operator-only wording (DGN-1873):
    # WORDING PENDING OWNER CONFIRMATION. No button (nothing to decide), no question.
    RETRY_TEXT="🔁 $(_agent_display)의 $(_job_display "$LABEL") 작업이 실패했지만 다음 정기 실행 때 자동으로 다시 해요.
다시 실패하면 그때 알려드릴게요.
>! 기술 상세
> 잡 라벨: ${LABEL}
> 단계: ${RETRY_WHAT:-?}
> 종료코드: ${EXIT_CODE}"
    # --no-owner-fallback: push.sh answers rc 9 (nothing sent) when
    # STEWARD_ENV_FILE resolves no operator -- same route as the dec-236
    # behind notice above.
    OP_RC=0
    "$PUSH_SH" --text "$RETRY_TEXT" ${PUSH_ENV_ARGS[@]+"${PUSH_ENV_ARGS[@]}"} --audience operator --no-owner-fallback >&2 || OP_RC=$?
    if [[ "$OP_RC" -eq 9 ]]; then
      echo "[cron-guard] retry-scheduled: no operator -- log only, the owner is never told" >&2
    elif [[ "$OP_RC" -ne 0 ]]; then
      echo "[cron-guard] operator retry-scheduled notice not delivered (push rc=$OP_RC)" >&2
    fi
    exit "$EXIT_CODE"
  fi
fi

# ---- DGN-835: usage-defer recovery branch (exit 75 + FRESH marker only) ----
# 2-factor: rc==75 alone is NOT enough (unrelated EX_TEMPFAIL must stay a
# visible failure); a stale marker (>10 min) is NOT enough either (an old
# defer must not hijack a later genuine failure).
if [[ "$EXIT_CODE" -eq 75 && -f "$USAGE_DEFER_MARKER" ]]; then
  MARKER_MTIME="$(file_mtime "$USAGE_DEFER_MARKER")"
  NOW_TS="$(date +%s)"
  if [[ -n "$MARKER_MTIME" ]] && [[ $(( NOW_TS - MARKER_MTIME )) -le 600 ]]; then
    # read window + reset epoch + last-success epoch from the marker
    # (best-effort; parse failure falls through to the normal alert path).
    DEFER_INFO="$(/usr/bin/python3 -c '
import json, sys
from datetime import datetime

def to_epoch(iso):
    try:
        return str(int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()))
    except Exception:
        return ""

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    window = d.get("window", "") or ""
    reset = d.get("reset_at", "") or ""
    last_success = d.get("last_success_at", "") or ""
    print(window + "|" + to_epoch(reset) + "|" + reset + "|" + to_epoch(last_success))
except Exception:
    print("")
' "$USAGE_DEFER_MARKER" 2>/dev/null)"
    DEFER_WINDOW="${DEFER_INFO%%|*}"
    _rest="${DEFER_INFO#*|}"
    DEFER_RESET_EPOCH="${_rest%%|*}"
    _rest="${_rest#*|}"
    DEFER_RESET_ISO="${_rest%%|*}"
    DEFER_LAST_SUCCESS_EPOCH="${_rest#*|}"

    # ---- shared owner-copy building blocks (7d notify + stale alert) ----
    if [[ -n "$FRIENDLY_NAME" ]]; then
      DEFER_HEADLINE="$FRIENDLY_NAME"
    else
      DEFER_HEADLINE="${LABEL##*.}"
    fi
    # Agent signature emoji: THE one resolver the restart-push prefix uses
    # (routines/lib/agent_prefix.py -- .instance.conf DOGANY_AGENT_PREFIX,
    # else persona '- Emoji:', else empty = no prefix). Never hardcode an
    # emoji here (owner rule: emoji = per-agent signature variable), never
    # fall back to a placeholder (DGN-828).
    AGENT_EMOJI="$(python3 "$AGENT_ROOT/routines/lib/agent_prefix.py" "$AGENT_ROOT" 2>/dev/null || true)"
    # Job display phrase (owner-approved copy 2026-08-12): the consolidate
    # label is CONSOLIDATE-ONLY wording. DEFER_WHAT = subject form (…가/이),
    # DEFER_WHAT_OBJ = object form (…를/을), so both approved sentences read
    # naturally. Any other usage-deferring job falls back to its friendly
    # name; registering a new job here means supplying its own owner-approved
    # phrases (UX gate).
    # DGN-851: the label text is locale data -- resolved from config/i18n
    # (cron.consolidate_defer_subject / _object); the in-code fallback is the
    # locked ko copy, so an instance without the keys is byte-identical.
    case "$LABEL" in
      *.consolidate-0430)
        DEFER_WHAT="$(i18n_or cron.consolidate_defer_subject "밤새 기억 정리가")"
        DEFER_WHAT_OBJ="$(i18n_or cron.consolidate_defer_object "밤새 기억 정리를")"
        ;;
      *)
        DEFER_WHAT="자동 작업 '${DEFER_HEADLINE}'이(가)"
        DEFER_WHAT_OBJ="자동 작업 '${DEFER_HEADLINE}'을(를)"
        ;;
    esac
    # human-readable reset time for the owner copy ({reset} slot)
    RESET_HUMAN=""
    if [[ -n "$DEFER_RESET_EPOCH" ]]; then
      RESET_HUMAN="$(date -r "$DEFER_RESET_EPOCH" '+%m/%d %H:%M' 2>/dev/null \
                     || date -d "@$DEFER_RESET_EPOCH" '+%m/%d %H:%M' 2>/dev/null || true)"
    fi
    [[ -z "$RESET_HUMAN" ]] && RESET_HUMAN="${DEFER_RESET_ISO:-unknown}"

    # defer_dedup_hit <kind>: per (label, kind, day) dedup marker. Returns 0
    # when already fired today (caller suppresses), else records and returns 1.
    defer_dedup_hit() {
      local f="$DEDUP_DIR/${LABEL}.$1.$(date +%Y%m%d)"
      [[ -f "$f" ]] && return 0
      mkdir -p "$DEDUP_DIR"
      touch "$f"
      return 1
    }

    # defer_push <push.sh args...>: push with the instance env when present.
    defer_push() {
      local PUSH_SH="$SCRIPT_DIR/push.sh"
      if [[ ! -x "$PUSH_SH" ]]; then
        echo "[cron-guard] push.sh not found for usage-defer notify: $PUSH_SH" >&2
        return 1
      fi
      if [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]]; then
        "$PUSH_SH" "$@" --env "$ENV_PATH" >&2 || true
      else
        "$PUSH_SH" "$@" >&2 || true
      fi
    }

    # ---- DGN-835 MAJOR-C: stale-canon alert READER ----
    # The deferring job embeds its last-SUCCESS timestamp in the marker
    # (last_success_at, read from its own success artifact -- consolidate:
    # memory-engine/.consolidate-last-success). When this run is ALSO a
    # defer and the last success is older than D_STALE (1 day) + 6h grace,
    # push ONE stale alert (dedup per label/day). The grace keeps the FIRST
    # missed daily run (~24h) silent and fires on the second (~48h) --
    # "2 consecutive defers" per the locked spec. This reader and the 7d
    # notify dedup below ship as ONE SET: dedup alone would open a silence
    # window in which the owner never learns the canon is stale.
    STALE_AFTER_SEC=$(( 86400 + 6 * 3600 ))
    if [[ -n "$DEFER_LAST_SUCCESS_EPOCH" ]] \
       && [[ $(( NOW_TS - DEFER_LAST_SUCCESS_EPOCH )) -gt "$STALE_AFTER_SEC" ]]; then
      if defer_dedup_hit stale; then
        echo "[cron-guard] usage-defer stale alert suppressed (dedup): $LABEL" >&2
      else
        STALE_DAYS=$(( (NOW_TS - DEFER_LAST_SUCCESS_EPOCH) / 86400 ))
        # Owner-approved copy (형님 2026-08-12). DEFER_WHAT = the i18n
        # consolidate label for the consolidate job (the only stale-eligible
        # job today); other jobs fall back to their friendly-name phrase
        # (see DEFER_WHAT above).
        STALE_TEXT="${AGENT_EMOJI:+${AGENT_EMOJI} }${DEFER_WHAT} ${STALE_DAYS}일째 실행되지 못했어요. 사용량이 풀리면 재개돼요."
        defer_push --text "$STALE_TEXT" --audience owner
        echo "[cron-guard] usage-defer stale alert pushed (last success ${STALE_DAYS}d ago, $LABEL)" >&2
      fi
    fi

    if [[ "$DEFER_WINDOW" == "five_hour" ]]; then
      # 5h defer: fully automatic -- schedule a reset-aligned one-shot that
      # replays THIS cron-guard invocation. No owner interaction.
      RETRY_SH="$SCRIPT_DIR/usage-defer-retry.sh"
      if [[ -n "$DEFER_RESET_EPOCH" && -x "$RETRY_SH" ]] \
         && "$RETRY_SH" --label "$LABEL" --reset-epoch "$DEFER_RESET_EPOCH" \
              -- /bin/bash "$SCRIPT_DIR/cron-guard.sh" "${ORIG_ARGS[@]}"; then
        echo "[cron-guard] usage-defer (five_hour): reset-aligned retry scheduled for $DEFER_RESET_ISO ($LABEL)" >&2
      else
        echo "[cron-guard] usage-defer (five_hour): retry scheduling unavailable -- next regular run handles it (fail-open, $LABEL)" >&2
      fi
      exit "$EXIT_CODE"
    fi

    if [[ "$DEFER_WINDOW" == "seven_day" ]]; then
      # 7d defer: owner decision. Write a replay file the bridge can execute
      # on button tap / slash command (charset-validated label; ~/.dogany is
      # user-owned), then push a notification with a manual-retry button.
      REPLAY_FILE="$USAGE_DEFER_DIR/${LABEL}.replay"
      REPLAY_TMP="${REPLAY_FILE}.tmp"
      {
        echo "#!/bin/bash"
        echo "# generated by cron-guard.sh -- DGN-835 seven_day usage-defer manual replay"
        echo "# label: $LABEL"
        echo "# one-shot: self-delete before exec (rm-on-launch replaces a TTL;"
        echo "# bash already holds the script fd, so the unlink is race-free)."
        echo 'rm -f -- "$0"'
        printf 'exec /bin/bash %q' "$SCRIPT_DIR/cron-guard.sh"
        printf ' %q' "${ORIG_ARGS[@]}"
        printf '\n'
      } > "$REPLAY_TMP" && chmod 700 "$REPLAY_TMP" && mv "$REPLAY_TMP" "$REPLAY_FILE"

      # Button rides only while callback_data fits Telegram's 64-BYTE cap
      # (DGN-835 MAJOR-발견3; push.sh guards this too, defense in depth). The
      # body ALWAYS carries the /usageretry slash line (DGN-841 A) -- the
      # slash path never hits the bridge's 20-min STALE gate, so an expired
      # or dropped button still leaves a working route.
      CB_DATA="usageretry:${LABEL}"
      BUTTON_OPTS=()
      if [[ "${#CB_DATA}" -le 64 ]]; then  # label charset is ASCII -> chars == bytes
        BUTTON_OPTS=(--button "지금 실행::${CB_DATA}")
      fi

      # Owner-approved copy (형님 2026-08-12 13:43). The consolidate label
      # wording is consolidate-only -- see DEFER_WHAT above. Button label:
      # "지금 실행".
      DEFER_TEXT="${AGENT_EMOJI:+${AGENT_EMOJI} }${DEFER_WHAT} 주간 사용량 한도로 대기 중입니다. 사용량 리셋되면(${RESET_HUMAN}) 자동 재개돼요. 지금 바로 실행하려면 아래 버튼을 누르세요.
또는 /usageretry ${LABEL}"

      # Dedup per label/day: a daily job saturated for days would otherwise
      # re-notify every run. The stale reader above is the paired escape
      # hatch (they ship as one set -- see MAJOR-C comment).
      if defer_dedup_hit defer7d; then
        echo "[cron-guard] usage-defer (seven_day) notify suppressed (dedup): $LABEL" >&2
      else
        defer_push --text "$DEFER_TEXT" --audience owner ${BUTTON_OPTS[@]+"${BUTTON_OPTS[@]}"}
        echo "[cron-guard] usage-defer (seven_day): owner notified with manual-retry button ($LABEL)" >&2
      fi

      # DGN-841 B: fresh-button re-notify at the reset boundary. The original
      # button dies at the bridge's 20-min STALE gate long before a 7d reset,
      # so a reset-aligned one-shot re-sends a NEW message (fresh timestamp =
      # tappable button). Reuses the usage-defer-retry scheduler; per-label
      # dedup there replaces any previous pending re-notify.
      RETRY_SH="$SCRIPT_DIR/usage-defer-retry.sh"
      if [[ -n "$DEFER_RESET_EPOCH" && -x "$RETRY_SH" ]]; then
        # Owner-approved copy (형님 2026-08-12) + [지금 실행] button. The
        # slash line rides along as an expiry backstop (DGN-841 A) even
        # though the approved sentence itself does not name it.
        RENOTIFY_TEXT="${AGENT_EMOJI:+${AGENT_EMOJI} }주간 사용량이 리셋됐어요. ${DEFER_WHAT_OBJ} 지금 실행할 수 있습니다.
또는 /usageretry ${LABEL}"
        RENOTIFY_CMD=(/bin/bash "$SCRIPT_DIR/push.sh" --text "$RENOTIFY_TEXT" --audience owner)
        [[ ${#BUTTON_OPTS[@]} -gt 0 ]] && RENOTIFY_CMD+=("${BUTTON_OPTS[@]}")
        [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]] && RENOTIFY_CMD+=(--env "$ENV_PATH")
        if "$RETRY_SH" --label "${LABEL}.notify7d" --reset-epoch "$DEFER_RESET_EPOCH" \
             -- "${RENOTIFY_CMD[@]}"; then
          echo "[cron-guard] usage-defer (seven_day): fresh-button re-notify scheduled at reset ($LABEL)" >&2
        else
          echo "[cron-guard] usage-defer (seven_day): re-notify scheduling unavailable (fail-open, $LABEL)" >&2
        fi
      fi
      exit "$EXIT_CODE"
    fi
    # unknown window -> fall through to the normal alert path.
    echo "[cron-guard] usage-defer marker has unknown window '$DEFER_WINDOW' -- treating as normal failure" >&2
  fi
fi

# ---- failure path ----
TODAY="$(date +%Y%m%d)"
DEDUP_FILE="$DEDUP_DIR/${LABEL}.${TODAY}"

mkdir -p "$DEDUP_DIR"

if [[ -f "$DEDUP_FILE" ]]; then
  # already alerted today -- suppress
  echo "[cron-guard] suppressed (dedup): $LABEL exit=$EXIT_CODE" >&2
  exit "$EXIT_CODE"
fi

# ---- first failure today: build alert and push ----
# grab last 10 lines of log (best-effort; prefer stderr when non-empty, fall back to stdout)
# DGN-1582 (2): a log last written before this run started is another run's
# output -- attaching it is false evidence, so it is skipped and reported absent.
LOG_TAIL=""
LOG_SOURCE=""
LOG_STALE_NOTE=""
log_is_stale() {
  local mt
  mt="$(file_mtime "$1" 2>/dev/null)" || return 1
  [[ -n "$mt" && "$mt" -lt "$RUN_START_EPOCH" ]]
}
if [[ -s "$STDERR_LOG_PATH" ]]; then
  if log_is_stale "$STDERR_LOG_PATH"; then
    LOG_STALE_NOTE="$STDERR_LOG_PATH"
  else
    LOG_TAIL="$(tail -10 "$STDERR_LOG_PATH" 2>/dev/null || true)"
    LOG_SOURCE="$STDERR_LOG_PATH"
  fi
fi
if [[ -z "$LOG_TAIL" && -s "$LOG_PATH" ]]; then
  if log_is_stale "$LOG_PATH"; then
    LOG_STALE_NOTE="${LOG_STALE_NOTE:-$LOG_PATH}"
  else
    LOG_TAIL="$(tail -10 "$LOG_PATH" 2>/dev/null || true)"
    LOG_SOURCE="$LOG_PATH"
  fi
fi

# stderr headline: the caller-supplied friendly name, else the label itself
# (maintainer-facing; an unnamed job must not read as anonymous, DGN-1582 (3)).
HEADLINE="${FRIENDLY_NAME:-$LABEL}"

# Owner-approved copy (2026-09-27, DGN-1747). First person: the agent whose
# chat this lands in (the steward, else the owner's own agent) speaks, and it
# ASKS -- the old copy promised a follow-up investigation nobody runs. The
# ask button reaches that agent's session through
# the bot.py opt: handler; its text names agent + job so the turn can act.
# Machine detail (label, exit code, time) rides only the folded block; the
# log tail stays on stderr below.
AGENT_DISP="$(_agent_display)"
JOB_DISP="$(_job_display "$LABEL")"
ALERT="⚠️ ${AGENT_DISP}의 ${JOB_DISP} 작업이 방금 실패했어요.
원인을 확인할까요?
>! 기술 상세
> 잡 라벨: ${LABEL}
> 종료코드: ${EXIT_CODE}
> 시각: $(date '+%Y-%m-%d %H:%M:%S %Z')"
ALERT_BUTTON="${AGENT_DISP} ${JOB_DISP} 실패 원인 확인해줘::opt:1"

# DGN-1873 (owner directive 2026-10-06): an unattended (dawn) run never asks
# -- the owner is asleep and the button expires before morning. No question,
# no button: what failed, why (one line machine-classified from this run's
# own exit + log bytes + host wake time), and when it is tried again (the
# job's next scheduled fire, never a fixed hour) -- routines/lib/dawn_alarm.py.
# Owner-approved copy (2026-10-06, dec-252): the headline below, verbatim;
# the reason and retry lines are approved in dawn_alarm.py.
if [[ "$UNATTENDED" -eq 1 ]]; then
  DAWN_ALARM_PY="$SCRIPT_DIR/lib/dawn_alarm.py"
  DAWN_REASON="$(python3 "$DAWN_ALARM_PY" reason --rc "$EXIT_CODE" \
    --run-start "$RUN_START_EPOCH" --label "$LABEL" \
    --log "${STDERR_LOG_PATH}@${STDERR_LOG_OFFSET}" --log "${LOG_PATH}@${LOG_OFFSET}" \
    --usage-marker "$USAGE_DEFER_MARKER" 2>/dev/null)"
  DAWN_RETRY="$(python3 "$DAWN_ALARM_PY" retry --label "$LABEL" 2>/dev/null)"
  # dawn_alarm.py unreadable (a standalone test copy): the approved
  # no-class reason line, and no retry line rather than a guessed time.
  [[ -n "$DAWN_REASON" ]] || DAWN_REASON="원인을 아직 가리지 못했어요. 기록은 남겨 뒀어요."
  ALERT="⚠️ 새벽에 ${AGENT_DISP}의 ${JOB_DISP} 작업이 실패했어요.
${DAWN_REASON}${DAWN_RETRY:+
${DAWN_RETRY}}
>! 기술 상세
> 잡 라벨: ${LABEL}
> 종료코드: ${EXIT_CODE}
> 시각: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  ALERT_BUTTON=""
fi

# Maintainer-facing detail (never pushed).
{
  echo "[cron-guard] ROUTINE FAILED: ${HEADLINE}"
  echo "[cron-guard]   label : $LABEL"
  echo "[cron-guard]   exit  : $EXIT_CODE"
  echo "[cron-guard]   date  : $(date '+%Y-%m-%d %H:%M:%S %Z')"
  if [[ -n "$LOG_TAIL" ]]; then
    echo "[cron-guard]   --- log tail ($LOG_SOURCE) ---"
    printf '%s\n' "$LOG_TAIL"
  elif [[ -n "$LOG_STALE_NOTE" ]]; then
    echo "[cron-guard]   log tail omitted: no log from this run found (stale: $LOG_STALE_NOTE, last modified before run start)"
  fi
} >&2

# ---- write dedup marker BEFORE pushing (prevent duplicate on push failure) ----
touch "$DEDUP_FILE"

# ---- push alert ----
PUSH_SH="$SCRIPT_DIR/push.sh"
if [[ ! -x "$PUSH_SH" ]]; then
  echo "[cron-guard] push.sh not found or not executable: $PUSH_SH" >&2
  exit "$EXIT_CODE"
fi

ALERT_BUTTON_ARGS=()
[[ -n "$ALERT_BUTTON" ]] && ALERT_BUTTON_ARGS=(--button "$ALERT_BUTTON")
if [[ -n "$ENV_PATH" && -f "$ENV_PATH" ]]; then
  "$PUSH_SH" --text "$ALERT" --env "$ENV_PATH" --audience operator ${ALERT_BUTTON_ARGS[@]+"${ALERT_BUTTON_ARGS[@]}"} >&2 || true
else
  "$PUSH_SH" --text "$ALERT" --audience operator ${ALERT_BUTTON_ARGS[@]+"${ALERT_BUTTON_ARGS[@]}"} >&2 || true
fi

exit "$EXIT_CODE"
