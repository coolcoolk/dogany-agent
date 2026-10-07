#!/bin/bash
# update_apply.sh -- DGN-1031 slice 2+3+4: the WRITE path of the unified
# update loop (instance-scoped units + crew-atomic transactions + framework
# items + ATOMIC GROUPS + the single completion notice = the closed loop).
#
# CONSUMES the slice-1 planner (scripts/pack/update_plan.sh) through its
# machine-readable emit channel (DOGANY_PLAN_EMIT). Plan logic is NOT
# re-derived here; the planner's product is the plan. Application itself is
# delegated ENTIRELY to existing machinery (spec §0: no new install
# algorithm, standalone paths untouched):
#   - packs      -> scripts/pack/pack_install.sh --upgrade
#   - framework  -> <instance>/routines/self-update.sh --no-restart
#     (--no-restart is self-update.sh's OWN shipped local flag -- the
#     restart deferral needs zero modification of self-update.sh. The loop
#     pins the child to the PLANNED candidate via DOGANY_UPDATE_PIN, which
#     resolve_channel_tag honours by contract, so the plan-apply window can
#     never land a different tag than the one the plan gated.)
# This script owns ONLY the loop seam:
#   - gate assertion (fail-closed, spec §4 belt 1)
#   - atomic-unit bridge stop / restart policy (spec §2 중단·재시작 정책)
#   - crew-atomic T0~T-end transaction (spec §3 -- slice 3), generalized to
#     the ATOMIC GROUP boundary (spec §2 -- slice 4)
#   - framework restart deferral to loop end (spec §2 rule 1 -- slice 4)
#   - ONE completion result per run (slice 4; DGN-1709: agent-voiced)
#   - the §6 loud accounting (counts always printed; 0 is suspicion)
#
# SCOPE (spec §2 결정적 적용 순서 -- the full unified turn):
#   - framework items (FW)                             -> self-update.sh
#     delegation; restart DEFERRED to loop end (spec §2: "재시작은 루프
#     끝으로 유예"). A completed fw unit whose instance gets restarted by a
#     LATER completed unit is not restarted twice (one restart per agent);
#     a completed fw unit whose instance is held down by a later INCOMPLETE
#     unit stays down (the stay-down policy of that unit wins). The reverse
#     order (framework planned LAST, DGN-1417) is the same rule mirrored: a
#     completed pack/crew unit whose instance a LATER plan line stops again
#     defers its restart to the same loop-end flush (DGN-1710).
#   - PACK items with kind != kit and no provides_kit  -> APPLY per instance
#   - crew-scoped kit items (kind=kit / provides_kit)  -> crew-atomic apply
#     (slice 3: the WHOLE crew is ONE atomic unit, spec §3 T0~T-end)
#   - ATOMIC GROUP items (fw + crew pack, prefix-incompatible by planner
#     proof)                                           -> ONE atomic unit:
#     the slice-3 T0~T-end transaction generalized to the group boundary
#     (stop everything -> backup -> apply -> verify -> restart everything).
#     INTERNAL ORDER = packs FIRST, then framework. Rationale: update.sh
#     runs fw_reqframework_guard fail-closed at fw-apply time, evaluating
#     the INSTALLED pack set -- fw-first inside the group would be blocked
#     by the still-old pack range (the exact incompatibility that made the
#     group atomic). Pack-first lets the shipped guard see the advanced
#     pack set; the group has no live prefix anyway (all bridges are down),
#     and a residual violation still fail-closes as INCOMPLETE.
#     Supported composition: >=1 framework item + exactly ONE crew-scoped
#     pack item (today's only structurally producible group). Any other
#     composition ABORTs loudly -- never guessed at.
#
# ATOMIC-UNIT POLICY (spec §2, single source):
#   instance-scoped: one (pack, instance) application = one atomic unit.
#   crew-scoped: the WHOLE crew (all member instances) = ONE atomic unit --
#   never an individual member. The unit's bridge(s) are stopped BEFORE any
#   write. A COMPLETED unit restarts its own bridge(s) and counts as landed.
#   An INCOMPLETE unit (pack_install rc != 0, or a rc-0 run whose output
#   lacks the install-side gate PASS line, or a post-apply DOGANY_PACKS
#   record mismatch) is NEVER restarted -- its bridge(s) STAY DOWN (no
#   half-state returns to live; for a crew that means EVERY member bridge
#   stays down) and remaining units are NOT attempted (fail-fast; units
#   completed BEFORE the failure stay restarted -- the stop does not
#   propagate across atomic-unit boundaries).
#   REFUSED BEFORE WRITING (DGN-1895, instance-scoped pack units): a
#   pack_install rc 1 whose write-boundary fact (DOGANY_INSTALL_STATE_FILE)
#   reads writes=none -- e.g. the DGN-1109 content-drift guard -- is NOT
#   incomplete: nothing changed, so the unit restarts what it stopped right
#   away and counts as BLOCKED (record: "restarted": true). Any other
#   failure, or a missing/"begun" fact, stays INCOMPLETE and down.
#   STOP DEBT (DGN-1506 item 5): a stop that never reached a restart is this
#   run's to undo, not the owner's. _launchd_stop takes the watchdog out with
#   the bridge, so the instance's own recovery arm cannot fire; _rearm_stopped
#   therefore re-arms every root this run verifiably took units away from --
#   at loop end AND from the EXIT trap (ABORT/INT/TERM included). INCOMPLETE
#   roots are the one exception: stay-down still wins there.
#
# CREW TRANSACTION (spec §3, T0~T-end):
#   T0 (pre-entry, ZERO writes until every check passes):
#     - apply-time crew re-discovery: crew.conf members re-read fresh and
#       3-source cross-validated (crew.conf / DOGANY_PACKS <kit>@ entry /
#       readlink <root>/database/<kit>.db == $SHARED_HOME/crews/<kit>/<kit>.db).
#       ANY mismatch = ABORT. 0 members = ABORT (never a pass, §6).
#       plan-vs-conf member-set drift = ABORT (plan snapshot never trusted).
#     - per-member fresh installed versions must be IDENTICAL (an asymmetric
#       crew is suspected half-state) + stale-plan / downgrade guards.
#     - ONE payload extraction; every member installs from the same tree
#       (per-member version divergence structurally impossible).
#     - per-member pack_install --dry-run must PASS; belt-1 lint presence;
#       restart-target plists per member (0 = suspicion); disk headroom.
#     - stop ALL member bridges (verified). A stop failure = pre-entry ABORT
#       with zero writes; already-stopped members stay down conservatively
#       (same direction as the slice-2 preflight-failure policy) with manual
#       restart commands printed.
#     - ONE loop-level shared-DB backup (sqlite3 .backup, fixed naming),
#       taken AFTER the stop so it snapshots the quiesced pre-transaction
#       state -- and so a pre-entry abort provably wrote NOTHING.
#   Phase P: pack_install --prepare for EVERY member, in its OWN loop, right
#     after the lease DIRTY write and before T1 (DGN-1517 spec v1 §2.3(a)).
#     Stages database/.gen/<cand_tag>/ from the single extraction below (own
#     contract: touches nothing else live) -- for EVERY member before ANY
#     member's migrate, so the crew-lockstep guard's evidence class 3a
#     (pack_install.sh _kit_crew_lockstep_e1e2) can see every sibling's
#     staged generation once member #1 reaches migrate. A prepare failure
#     here is this member's INCOMPLETE, same shape as any T1..TN failure.
#   T1..TN: per member, IN THE SAME ITERATION (DGN-1509 S5/S7 Phase 1/S5b --
#     "... migrate-once -> activate -> verify"; prepare already ran in
#     Phase P above, NOT repeated here):
#       (a) pack_install --upgrade -- delivers $cand_ver live (unchanged
#           mechanism). The shared crew DB migrates EXACTLY ONCE across the
#           whole loop: member #1's kit-migrate advances PRAGMA user_version
#           to the code pin THROUGH the symlink; every later member re-reads
#           cur==pin and skips the apply loop (pack_install.sh kit-migrate
#           contract), with the endpoint check still asserting sync. No
#           loop-side migration logic exists here at all.
#       (b) post-install verify (DOGANY_PACKS record + evidence receipt).
#       (c) activate + layout-migrate -- flips database/.gen-active to the
#           generation Phase P staged (content-identical to what (a) just
#           wrote live, same extraction) and converts this member's real
#           files into the permanent database/<name> -> .gen-active/<name>
#           symlink layout (idempotent past the first run), so the NEXT
#           crew transaction finds this member already on that layout
#           (branch1 skip, S5b §2.3) and can activate with a single flip.
#     A failure at ANY of (a)-(c) is this member's INCOMPLETE -- members
#     before it in the loop already fully completed Phase P + (a)-(c);
#     members after it are never attempted (the established
#     whole-crew-stays-down shape, unchanged by this addition).
#     DGN-1436 "KNOWN OVER-BLOCK" (pack_install.sh _kit_crew_lockstep_guard)
#     used to block a genuine >=2-member joint upgrade at member #1's
#     migrate, because sibling members' on-disk pins had not changed yet at
#     that point. DGN-1517 (spec v1) MEASURED the
#     documented fix -- activate EVERY member's generation before any
#     member's migrate -- and found it incompatible with the landed
#     layout-migrate semantics (crew_layout.sh branch2 silently overwrites a
#     staged NEW payload with the OLD live file when activation precedes
#     migrate), not merely risky/untested. The fix actually landed instead:
#     Phase P above (every member's generation staged, not activated, before
#     any migrate) plus a second evidence class in the guard itself (lease +
#     staged generation prove a sibling is mid-flight in the SAME
#     transaction, without touching what any live runtime reads). See that
#     spec's §1.2/§2 for the full argument and §2.5 for rejected
#     alternatives.
#   T-end: per-member DOGANY_PACKS record verified (both strings printed),
#     THEN all member bridges restart. Any earlier failure -> every crew
#     bridge stays down + T0 backup path and manual recovery steps printed.
#
# GATES (all fail-closed):
#   - planner rc != 0                    -> ABORT (zero application)
#   - compat-lint.sh absent/unreadable   -> ABORT at entry + re-assert per
#     unit immediately before pack_install (spec §4 belt 1). The lint RUN
#     itself is pack_install's install-side gate -- invoked exactly once per
#     unit (a second loop-level run would double-invoke the linter on the
#     same payload; see DGN-1036).
#   - success judgment (DGN-1682): pack_install rc + DOGANY_PACKS record +
#     delivery receipt. DOGANY_STRICT_GATES=1 is exported to every installer
#     call and makes pack_install itself exit nonzero when its compat-lint
#     gate would be skipped (spec §4 belt 2, live since DGN-1682).
#   - belt-2 output scan (defense in depth): a pack_install rc-0 run whose
#     own top-level output lacks the compat-lint PASS line of ITS path
#     (legacy "compat-lint: install-side gate PASS", kind=pack "pack:
#     compat-lint PASS", kind=kit "kit: compat-lint PASS" -- see
#     _install_gate_passed) is treated as INCOMPLETE -- never as success.
#   - downgrade guard: candidate < installed AT APPLY TIME (re-read fresh
#     from .instance.conf, never trusted from the plan snapshot) -> BLOCKED,
#     never applied. candidate == installed -> BLOCKED-STALE-PLAN (the plan
#     aged between plan and apply -- suspicious, loud, not silent success).
#     Comparison runs through scripts/pack/lib/semver_range.py (single
#     truth source -- no new semver logic).
#   - extracted payload manifest id/kind/provides/pack_version asserts
#     (subscription-poisoning + crew-scope defense in depth).
#   - restart-target plists 0건 -> BLOCKED (0 is suspicion, §6).
#
# NOTIFICATION (slice 4 + sender/abort fix): AT MOST ONE push per run --
# normal completion OR abort, never both. Body = §6 counts (완주/차단/
# 미완주/재시작); an INCOMPLETE unit MUST surface in the notice (a run with
# any incomplete unit can never read as a clean success). SENDER IS FIXED:
# the notice leaves from the operator instance (slug match via
# .instance.conf DOGANY_AGENT_NAME == DOGANY_APPLY_NOTIFY_SLUG, default
# "metal") -- never from whichever plan item came first (predictable sender;
# domain agents are not infra-report channels). Operator unresolvable ->
# LOUD WARN + full body into the run log; NO proxy send through another
# instance. ABORT paths send one notice too (EXIT trap): reason, best-known
# counts (marked non-final when the walk did not finish), and which bridges
# remain STOPPED -- an update that dies half-way can never die silently.
# Push path = the existing per-instance routines/push.sh core
# (dogany-proactive-push; watchdog.sh precedent) -- no new push channel.
# DGN-1757: update_surface renders and sends the structured result. Only a
# non-done result reaches the owner agent for an optional single reason line.
# If the surface module is unavailable, the legacy neutral fallback remains.
#
# --dry-run: full plan consumption, per-unit guard evaluation and intended
# actions printed, pack_install --dry-run preflight per applicable unit.
# Framework units print the intended self-update command WITHOUT invoking it
# (self-update.sh performs a `git fetch --tags` against the shared repo even
# under --dry-run -- a network op and a ref write outside this loop's
# zero-write promise). ZERO instance writes, ZERO launchctl calls, ZERO
# restarts, ZERO pushes (extraction goes to this script's private tmp only,
# removed on exit).
#
# CREW SYNC (dec-263 (a), the owner's [버전 맞추기]): a crew PACK line may
# carry a 9th field, the per-member installed versions the planner planned
# it from (update_plan.sh DOGANY_PLAN_CREW_SYNC, "name=ver ..."). That one
# line is the only way an ASYMMETRIC crew enters the transaction: T0-2 then
# requires every member's fresh version to equal its declared one, every
# member to sit on a stable tag of the registry repo (an untagged or
# pre-release trial install is operator-only), and the candidate to be >=
# every member and > at least one (no downgrade, no silent no-op). The
# kit-core pin-line probe is skipped for it (a split crew's pins differ by
# definition); every other T0 check, the consent token, the lease, Phase P
# and the per-member receipts are the same transaction. Not supported inside
# an ATOMIC group or on a non-crew line (ABORT, fail-closed).
#
# --plan-file <f> / --result-file <f> (DGN-1417 update ledger): apply exactly
# the ledger-selected plan lines, and hand the single result record to the
# ledger supervisor (which delivers it after the final restart and its own
# verification) instead of delivering it here.
#
# Env (TEST HERMETICITY / rehearsal ONLY -- update_plan.sh doctrine):
#   DOGANY_SHARED_HOME / DOGANY_PACK_SOURCES / DOGANY_CREW_DIR /
#   DOGANY_PLAN_INSTANCES   passed through to the planner; registry lookup
#                           uses the same resolution
#   DOGANY_LAUNCHD_CAPTURE  rehearsal file (pack_install convention): launchd
#                           stop/restart commands are CAPTURED there, not
#                           executed (the completion notice is NOT captured
#                           here -- notice delivery goes through the resolved
#                           push.sh, which tests stub via
#                           DOGANY_APPLY_NOTIFY_ROOT)
#   DOGANY_PACK_CATALOG     catalog override passed to pack_install --catalog
#                           (update_seed.sh precedent; unset = installer
#                           default, i.e. the framework repo catalog)
#   DOGANY_APPLY_PLAN_FILE  consume this emit file instead of running the
#                           planner (guard-path tests only)
#   DOGANY_APPLY_INSTALLER  pack_install.sh override (belt-2 scan test only)
#   DOGANY_APPLY_SELFUPDATE self-update.sh override (fw failure-injection
#                           tests only; unset = <root>/routines/self-update.sh)
#   DOGANY_APPLY_NOTIFY_ROOT  instance root whose routines/push.sh sends the
#                           single completion/abort notice (test stub seam;
#                           unset = fixed-sender resolution below)
#   DOGANY_APPLY_NOTIFY_SLUG  operator-instance slug the fixed-sender
#                           resolution matches against .instance.conf
#                           DOGANY_AGENT_NAME (default: metal). Infra notices
#                           always leave from the OPERATOR instance -- never
#                           from whichever plan item happened to come first
#                           (sender must be predictable for the owner; domain
#                           agents are not infra-report channels)
#   DOGANY_APPLY_STOP_POLL_TRIES / DOGANY_APPLY_STOP_POLL_INTERVAL
#                           bounded-poll knobs for stop verification
#                           (DGN-1640); tests shrink both to run fast
#
# Exit codes:
#   0 = every applicable unit landed and restarted (or applicable set is
#       legitimately empty with loud per-item reasons)
#   2 = ABORT / any BLOCKED / any INCOMPLETE / restart failure / accounting
#       mismatch
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# DGN-1424: resolve the python3 interpreter ONCE, PATH-independent (a bare
# `python3` call resolves whatever the CALLER's PATH hands back -- see the
# lib file header). Exported so PLANNER/INSTALLER/MINT_RUN/COMPAT_LINT below
# (each a separate `bash` subprocess) reuse the SAME resolution.
PY_RESOLVE_LIB="$SCRIPT_DIR/lib/py_resolve.sh"
[[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
  || { echo "ERROR: python resolver library missing ($PY_RESOLVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/py_resolve.sh
. "$PY_RESOLVE_LIB"
pack_resolve_py3 || exit 1

PLIST_LABEL_LIB="$SCRIPT_DIR/lib/plist_label.sh"
[[ -f "$PLIST_LABEL_LIB" && -r "$PLIST_LABEL_LIB" ]] \
  || { echo "ERROR: plist Label library missing ($PLIST_LABEL_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/plist_label.sh
. "$PLIST_LABEL_LIB"

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
SOURCES_CONF="${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
PLANNER="$SCRIPT_DIR/update_plan.sh"
INSTALLER="${DOGANY_APPLY_INSTALLER:-$SCRIPT_DIR/pack_install.sh}"
# Restart machine. Overridable for the same reason DOGANY_APPLY_INSTALLER is:
# a self-test that exercises the re-arm path must be able to observe the call
# without a fixture plist ever reaching live launchd (mint_run.sh start copies
# into ~/Library/LaunchAgents and bootstraps).
MINT_RUN="${DOGANY_APPLY_MINT_RUN:-$SCRIPT_DIR/mint_run.sh}"
# Bounded-poll knobs for stop verification (DGN-1640): `launchctl bootout`
# returning 0 means ACCEPTED, not FINISHED -- an exiting KeepAlive process is
# still listed for a short window afterward. An IDLE bridge exits in under a
# second, but a bridge with a live turn runs its graceful shutdown (fold flush,
# SDK teardown) first: measured 2026-09-27 08:43, one bridge took ~40s, so the old
# 2s bound (10 x 0.2s) failed the stop of every busy bridge (two busy ones
# BLOCKED, idle health passed; DGN-1739). Bound = 300 x 0.2s = 60s, above
# launchd's default 20s ExitTimeOut plus the graceful flush. Overridable so a
# test can shrink the bound instead of paying it for real (launchctl itself is
# stubbed via PATH, same as the neighbouring launchd suites).
STOP_POLL_TRIES="${DOGANY_APPLY_STOP_POLL_TRIES:-300}"
STOP_POLL_INTERVAL="${DOGANY_APPLY_STOP_POLL_INTERVAL:-0.2}"
COMPAT_LINT="$SCRIPT_DIR/compat-lint.sh"
SEMVER_LIB="$SCRIPT_DIR/lib/semver_range.py"
CAPTURE="${DOGANY_LAUNCHD_CAPTURE:-}"
PLAN_FILE_OVERRIDE="${DOGANY_APPLY_PLAN_FILE:-}"
PLAN_FILE_PRODUCT=""
RESULT_FILE_PRODUCT=""  # DGN-1417: ledger supervisor owns delivery (--result-file)
CATALOG_OVERRIDE="${DOGANY_PACK_CATALOG:-}"
SELFUPD_OVERRIDE="${DOGANY_APPLY_SELFUPDATE:-}"
NOTIFY_ROOT_OVERRIDE="${DOGANY_APPLY_NOTIFY_ROOT:-}"
NOTIFY_SLUG="${DOGANY_APPLY_NOTIFY_SLUG:-metal}"

# Installer arg vector shared by dry-run and real paths (single source).
CATALOG_ARGS=()
if [[ -n "$CATALOG_OVERRIDE" ]]; then
  CATALOG_ARGS=(--catalog "$CATALOG_OVERRIDE")
fi

# Recovery is a separate entrypoint: no planner, stops, installs or notices.
if [[ "${1:-}" == "--clear-incomplete" ]]; then
  [[ "$#" -eq 2 && "$2" =~ ^[A-Za-z0-9_-]+$ ]] || {
    echo "usage: update_apply.sh --clear-incomplete <kit>" >&2; exit 2;
  }
  . "$SCRIPT_DIR/lib/crew_lease.sh"
  _crew_lease_verify_clear "$2"
  exit $?
fi

DRY=0
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --dry-run|--dry) DRY=1; shift ;;
    # DGN-1417 product seam.  Unlike DOGANY_APPLY_PLAN_FILE (a deliberately
    # test-only env injection), this is an explicit, immutable plan selected
    # by the durable update ledger. Real runs verify the supervisor handoff
    # and selected bytes below; a filename alone grants no authority.
    --plan-file)
      [[ "$#" -ge 2 && -f "$2" ]] || { echo "--plan-file needs a readable plan" >&2; exit 2; }
      PLAN_FILE_PRODUCT="$2"; shift 2 ;;
    # DGN-1417 post-restart delivery: the update ledger's supervisor runs
    # outside the stopped set and sends the per-item result AFTER the final
    # restart and its own verification. With this flag the run's single
    # result record is handed to that file instead of being delivered here
    # (still exactly one delivery per run -- the supervisor's).
    --result-file)
      [[ "$#" -ge 2 && -n "$2" && -d "$(dirname "$2")" ]] || { echo "--result-file needs a path in an existing directory" >&2; exit 2; }
      RESULT_FILE_PRODUCT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$PLAN_FILE_PRODUCT" && -n "$PLAN_FILE_OVERRIDE" ]]; then
  echo "--plan-file cannot be combined with DOGANY_APPLY_PLAN_FILE" >&2; exit 2
fi

# _log also keeps the latest per-unit BLOCKED / 미완주 line as UNIT_DETAIL
# (DGN-1709: machine detail next to the plain reason in the result record).
_log()   {
  echo "[apply] $*"
  case "$*" in *": BLOCKED"*|*": 미완주 --"*) UNIT_DETAIL="${*#"${*%%[![:space:]]*}"}" ;; esac
}
_abort() { LAST_ABORT_MSG="$*"; echo "[apply] ABORT: $*" >&2; exit 2; }

# _gate_stop <gate-id> <operator reason...> -- DGN-1506 item 3, (a)-leg
# parity with pack_install.sh's _gate_stop: a PLANNED, user-reachable
# refusal records WHICH gate fired as a machine fact when the caller set
# DOGANY_GATE_STATE_FILE (a wrapper transaction reads the marker to send the
# gate's user_hint from gates.json without the model). Unlike the
# installer's, this helper does NOT exit: update_apply's refusal unit is
# BLOCKED (`return 1` at the call site) and the ONE-notice-per-run lane
# (_notify_send) stays this script's user surface. Registered ids are
# cross-checked against gates.json by scripts/user-gate-lint.py (C2).
_gate_stop() {
  local gid="$1"; shift
  if [[ -n "${DOGANY_GATE_STATE_FILE:-}" ]]; then
    printf 'gate_id=%s\noperator_reason=%s\n' "$gid" "$*" \
      > "$DOGANY_GATE_STATE_FILE" 2>/dev/null \
      || _log "WARN: gate marker unwritable ($DOGANY_GATE_STATE_FILE) -- refusal proceeds without the marker"
  fi
  _log "$* [gate:$gid]"
  return 1
}

_conf_get() { # <conf> <key>
  sed -n "s|^$2=||p" "$1" 2>/dev/null | head -n1
}

# Shared estate discovery (DGN-1037 machinery, extracted to a lib): the
# fixed-sender resolution scans the SAME instance set as the planner.
DISCOVER_LIB="$SCRIPT_DIR/lib/discover_instances.sh"
[[ -f "$DISCOVER_LIB" && -r "$DISCOVER_LIB" ]] \
  || _abort "인스턴스 탐지 라이브러리 부재 ($DISCOVER_LIB) -- fail-closed"
# shellcheck source=lib/discover_instances.sh
. "$DISCOVER_LIB"

# DGN-1509 B2: on-disk crew-transaction lease (pessimistic dirty write +
# read). _apply_crew is the sole producer; _rearm_stopped below is the
# consumer that closes the signal-interruption hole (see lib file header).
CREW_LEASE_LIB="$SCRIPT_DIR/lib/crew_lease.sh"
[[ -f "$CREW_LEASE_LIB" && -r "$CREW_LEASE_LIB" ]] \
  || _abort "크루 lease 라이브러리 부재 ($CREW_LEASE_LIB) -- fail-closed (DGN-1509)"
# shellcheck source=lib/crew_lease.sh
. "$CREW_LEASE_LIB"

# DGN-1509 S7: generation activate (S7 Phase 1) + layout migration (S5b)
# primitives -- _apply_crew is the first caller of either (see each lib's
# own header for why neither was wired at landing time).
CREW_ACTIVATE_LIB="$SCRIPT_DIR/lib/crew_activate.sh"
[[ -f "$CREW_ACTIVATE_LIB" && -r "$CREW_ACTIVATE_LIB" ]] \
  || _abort "크루 activate 라이브러리 부재 ($CREW_ACTIVATE_LIB) -- fail-closed (DGN-1509)"
# shellcheck source=lib/crew_activate.sh
. "$CREW_ACTIVATE_LIB"
CREW_LAYOUT_LIB="$SCRIPT_DIR/lib/crew_layout.sh"
[[ -f "$CREW_LAYOUT_LIB" && -r "$CREW_LAYOUT_LIB" ]] \
  || _abort "크루 layout 라이브러리 부재 ($CREW_LAYOUT_LIB) -- fail-closed (DGN-1509)"
# shellcheck source=lib/crew_layout.sh
. "$CREW_LAYOUT_LIB"

# DGN-1506 item 2: on-disk crew-pause consent token (recorded by the
# separate approval verb scripts/pack/crew_consent.sh in the owner
# conversation; verified+consumed by _apply_crew's crew.consent gate --
# a record comparison, never the model's recollection).
CREW_CONSENT_LIB="$SCRIPT_DIR/lib/crew_consent.sh"
[[ -f "$CREW_CONSENT_LIB" && -r "$CREW_CONSENT_LIB" ]] \
  || _abort "크루 동의 토큰 라이브러리 부재 ($CREW_CONSENT_LIB) -- fail-closed (DGN-1506)"
# shellcheck source=lib/crew_consent.sh
. "$CREW_CONSENT_LIB"

# Notification state -- one run sends AT MOST one push (normal OR abort).
NOTIFY_SENT=0        # any push attempted (success or push-rc-fail) this run
RUN_COMPLETED=0      # set immediately before the normal final exit
LAST_ABORT_MSG=""    # last explicit _abort reason (abort-notice body)
STOPPED_PENDING=""   # roots this run stopped and has not restarted (yet)

# _resolve_notify_push -- fixed-sender resolution (sender defect fix).
# Sets NOTIFY_PUSH (executable push.sh path, '' = unresolved) and
# NOTIFY_PUSH_WHY (loud reason when unresolved). Read-only; never aborts
# (also runs inside the EXIT trap). Resolution order:
#   1. DOGANY_APPLY_NOTIFY_ROOT override (test stub seam -- kept)
#   2. DOGANY_PLAN_INSTANCES entries (hermetic runs scan the SAME candidate
#      set the planner saw -- a fixture run must never scan the real HOME)
#   3. _discover_instances HOME estate scan (shared DGN-1037 machinery)
# Match = .instance.conf DOGANY_AGENT_NAME == NOTIFY_SLUG. Multiple matches
# (misconfig) -> WARN + first in deterministic sorted order. NO fallback to
# other instances: domain agents are not infra-report channels.
# DGN-1709: also sets NOTIFY_ROOT -- the owning instance root whose
# session-inbox receives the result record ('' = unresolved).
NOTIFY_PUSH=""
NOTIFY_PUSH_WHY=""
NOTIFY_ROOT=""
_resolve_notify_push() {
  NOTIFY_PUSH=""; NOTIFY_PUSH_WHY=""; NOTIFY_ROOT=""
  local entries="" e root slug matches="" m n=0
  if [[ -n "$NOTIFY_ROOT_OVERRIDE" ]]; then
    [[ -d "$NOTIFY_ROOT_OVERRIDE" ]] && NOTIFY_ROOT="$NOTIFY_ROOT_OVERRIDE"
    if [[ -x "$NOTIFY_ROOT_OVERRIDE/routines/push.sh" ]]; then
      NOTIFY_PUSH="$NOTIFY_ROOT_OVERRIDE/routines/push.sh"
    else
      NOTIFY_PUSH_WHY="오버라이드 루트($NOTIFY_ROOT_OVERRIDE)에 실행 가능한 routines/push.sh 없음"
    fi
    return 0
  fi
  if [[ -n "${DOGANY_PLAN_INSTANCES:-}" ]]; then
    entries="${DOGANY_PLAN_INSTANCES//,/ }"
  else
    if ! entries="$(_discover_instances | tr '\n' ' ')"; then
      NOTIFY_PUSH_WHY="인스턴스 스캔 불가 (HOME 미설정/비실재)"
      return 0
    fi
  fi
  for e in $entries; do
    root="${e#*:}"
    [[ -f "$root/.instance.conf" ]] || continue
    # '|| true': an unreadable conf must not errexit-kill the resolver
    # (this also runs inside the EXIT trap) -- it just fails the match.
    slug="$(_conf_get "$root/.instance.conf" DOGANY_AGENT_NAME || true)"
    if [[ -n "$slug" && "$slug" == "$NOTIFY_SLUG" ]]; then
      matches="${matches:+$matches }$root"
      n=$((n + 1))
    fi
  done
  if [[ "$n" -eq 0 ]]; then
    NOTIFY_PUSH_WHY="발신자 인스턴스 미발견 -- 스캔 후보에 슬러그 '$NOTIFY_SLUG' 없음"
    return 0
  fi
  if [[ "$n" -gt 1 ]]; then
    _log "WARN: 발신자 슬러그 '$NOTIFY_SLUG' 다중 매치 ${n}건 -- 정렬 순서상 첫 매치 사용 (결정적)"
  fi
  NOTIFY_ROOT="${matches%% *}"
  for m in $matches; do
    if [[ -x "$m/routines/push.sh" ]]; then
      NOTIFY_ROOT="$m"
      NOTIFY_PUSH="$m/routines/push.sh"
      return 0
    fi
  done
  NOTIFY_PUSH_WHY="슬러그 '$NOTIFY_SLUG' 인스턴스는 있으나 실행 가능한 routines/push.sh 없음"
  return 0
}

# ---------------------------------------------------------------------------
# DGN-1709: the run's result goes to the owning AGENT, not to the owner as
# fixed copy. The old notice pushed a machine-written body under a
# "COPY NOT LOCKED" placeholder marker (dec-094) that was never finalized, so
# the owner received the raw marker plus internal counters -- and its header
# was chosen by the incomplete count alone, so 0 completed / 1 blocked read
# "update complete". Owner direction (09-25): no hardcoded owner copy; the
# agent speaks.
#
# So the machine now emits ONE structured result record (outcome + per-unit
# result with a plain-terms reason + restart failures + bridges left down)
# and delivers it the way other machine events reach the agent: a DGN-217
# session-inbox drop into the owning instance (temp write + atomic rename to
# *.md; bot.py injects it as a background turn and the AGENT tells the owner
# in its own words). Only when no agent path exists for this run (no
# resolvable owning instance, no .telegram_bot/, or this very run left that
# instance's bridge down / failed to restart it) does it fall back to a
# minimal neutral push: no placeholder marker, no internal counter names.
# One run still produces AT MOST one delivery (NOTIFY_SENT).
# ---------------------------------------------------------------------------

# Per-unit result log for the record: one line per atomic unit,
# unit<US>result<US>reason<US>detail. UNIT_DETAIL is the last BLOCKED /
# 미완주 log line of the unit being walked (captured by _log), kept as the
# machine detail next to the plain reason.
# DGN-1746: UNIT_META (kind<US>id<US>cand_tag<US>members "name:root ...") is
# set by the walk before a unit's _unit_rec and rides along as fields 5-8, so
# the record can name the unit through update_surface's display seat
# (product_name / member_label) instead of handing the agent bare slugs.
# DGN-1895: UNIT_REFUSAL (restarted|restart_failed) is set by _apply_unit
# when pack_install refused before its write boundary and the unit restarted
# what it had stopped; it rides along as field 11 ("restarted" in the record)
# and picks the plain reason. Consumed and cleared here like UNIT_DETAIL.
UNIT_RECS=""
UNIT_DETAIL=""
UNIT_META=""
UNIT_REFUSAL=""
_unit_rec() { # <unit> <ok|blocked|incomplete|untried> [plain reason override] [dependency id]
  local unit="$1" result="$2" reason="${3:-}" detail refusal="${UNIT_REFUSAL:-}"
  UNIT_REFUSAL=""
  if [[ -z "$reason" && "$result" == "blocked" ]]; then
    case "$refusal" in
      restarted)      reason="not changed: the update refused itself before writing anything; this agent was switched back on" ;;
      restart_failed) reason="not changed: the update refused itself before writing anything; switching this agent back on failed" ;;
    esac
  fi
  if [[ -z "$reason" ]]; then
    case "$result" in
      ok)         reason="updated" ;;
      blocked)    reason="not changed: a safety check stopped it before anything was touched" ;;
      incomplete) reason="the update started but did not finish; this agent was left switched off so it does not run half-updated" ;;
      untried)    reason="not attempted: an earlier update in this same run did not finish" ;;
    esac
  fi
  detail="$UNIT_DETAIL"
  [[ "$result" == "ok" || "$result" == "untried" ]] && detail=""
  UNIT_RECS="${UNIT_RECS}${unit}"$'\x1f'"${result}"$'\x1f'"${reason}"$'\x1f'"${detail}"$'\x1f'"${UNIT_META}"$'\x1f'"${UNIT_TARGET:-}"$'\x1f'"${4:-}"$'\x1f'"${refusal}"$'\n'
  UNIT_DETAIL=""
  return 0
}

# _apply_outcome [aborted] -- the ONE classification of a run. "done" only
# when EVERY target unit completed (and every restart landed -- a completed
# apply with a dead bridge is not a success); nothing completed and every
# unit stopped cleanly before a write = "blocked"; any other finished run is
# "partial" (the per-unit list says which part). 0 ok + 1 blocked = blocked.
_apply_outcome() {
  if [[ "${1:-}" == "aborted" ]]; then echo aborted; return 0; fi
  if [[ "${N_TARGET:-0}" -gt 0 && "${N_OK:-0}" -eq "${N_TARGET:-0}" && "${N_RESTART_FAIL:-0}" -eq 0 ]]; then
    echo done
  elif [[ "${N_OK:-0}" -eq 0 && "${N_INCOMPLETE:-0}" -eq 0 && "${N_UNTRIED:-0}" -eq 0 && "${N_BLOCK:-0}" -gt 0 ]]; then
    echo blocked
  else
    echo partial
  fi
}

# _names_of <roots...> -- deduped basenames, space separated.
_names_of() {
  local r n out=""
  for r in "$@"; do
    n="$(basename "$r")"
    case " $out " in *" $n "*) : ;; *) out="${out:+$out }$n" ;; esac
  done
  printf '%s' "$out"
}

# _left_down_roots -- roots whose bridge THIS run leaves switched off:
# INCOMPLETE units (stay-down policy) + VERIFIED stops that never reached a
# restart (STOPPED_PENDING ∩ TORNDOWN_ROOTS, DGN-1511: an attempted-but-
# unverified stop is NOT a stop -- those go to the unconfirmed list).
_left_down_roots() {
  local r out=""
  for r in ${DOWN_ROOTS:-}; do
    case " $out " in *" $r "*) : ;; *) out="${out:+$out }$r" ;; esac
  done
  for r in ${STOPPED_PENDING:-}; do
    case " ${TORNDOWN_ROOTS:-} " in *" $r "*) ;; *) continue ;; esac
    case " $out " in *" $r "*) : ;; *) out="${out:+$out }$r" ;; esac
  done
  printf '%s' "$out"
}

# _result_record <outcome> [abort-reason] -- print the JSON record. Built by
# python so every string is escaped and the output always decodes as clean
# UTF-8 (bot.py quarantines undecodable inbox files).
_result_record() {
  local outcome="$1" abort_reason="${2:-}" final=1
  [[ "$outcome" == "aborted" ]] && final=0
  R_OUTCOME="$outcome" R_ABORT="$abort_reason" R_FINAL="$final" \
  R_TALLY="$([[ -n "${N_TARGET+x}" ]] && echo 1 || echo 0)" \
  R_TARGET="${N_TARGET:-0}" R_OK="${N_OK:-0}" R_BLOCK="${N_BLOCK:-0}" \
  R_INCOMPLETE="${N_INCOMPLETE:-0}" R_UNTRIED="${N_UNTRIED:-0}" \
  R_RESTART="${N_RESTART:-0}" R_RESTART_FAIL="${N_RESTART_FAIL:-0}" \
  R_UNITS="$UNIT_RECS" \
  R_DOWN="$(_names_of $(_left_down_roots))" \
  R_RFAIL="$(_names_of ${RESTART_FAILED_ROOTS:-})" \
  R_SFAIL="$(_names_of ${STOP_FAILED_ROOTS:-})" \
  R_DOWN_ROOTS="$(_left_down_roots)" \
  R_RFAIL_ROOTS="${RESTART_FAILED_ROOTS:-}" \
  R_SFAIL_ROOTS="${STOP_FAILED_ROOTS:-}" \
  R_SURFACE="${SCRIPT_DIR:-}/update_surface.py" \
  R_REPO="$(cd "${SCRIPT_DIR:-.}/../.." 2>/dev/null && pwd)" \
  R_LANG_ROOT="${NOTIFY_ROOT:-}" \
  DOGANY_SHARED_HOME="${SHARED_HOME:-${DOGANY_SHARED_HOME:-$HOME/.dogany}}" \
  DOGANY_PACK_SOURCES="${SOURCES_CONF:-${DOGANY_PACK_SOURCES:-$HOME/.dogany/pack-sources.conf}}" \
  R_DRY="${DRY:-0}" \
    "${PACK_PY3:-python3}" - <<'PY'
import json, os
def env(k):
    return os.environ.get(k, "").encode("utf-8", "replace").decode("utf-8", "replace")
def num(k):
    try:
        return int(env(k))
    except ValueError:
        return 0
units, metas = [], []
for line in env("R_UNITS").split("\n"):
    if not line:
        continue
    f = (line.split("\x1f") + [""] * 11)[:11]
    u = {"unit": f[0], "result": f[1], "reason": f[2]}
    if f[3].strip():
        u["detail"] = f[3].strip()
    if f[10] == "restarted":  # DGN-1895: refused before writing, switched back on
        u["restarted"] = True
    units.append(u)
    members = []
    for m in f[7].split():
        name, _, root = m.partition(":")
        members.append((name, root))
    metas.append({"kind": f[4], "id": f[5], "cand_tag": f[6], "members": members, "target": f[8], "dependency": f[9]})
rec = {
    "kind": "update_apply_result",
    "outcome": env("R_OUTCOME"),
    "final": env("R_FINAL") == "1",
    "dry_run": env("R_DRY") == "1",
    "units": units,
    "restart_failures": env("R_RFAIL").split(),
    "bridges_left_down": env("R_DOWN").split(),
    "bridges_state_unconfirmed": env("R_SFAIL").split(),
}
if env("R_TALLY") == "1":
    rec["counts"] = {k: num(v) for k, v in (
        ("target", "R_TARGET"), ("completed", "R_OK"), ("blocked", "R_BLOCK"),
        ("incomplete", "R_INCOMPLETE"), ("not_attempted", "R_UNTRIED"),
        ("restarted", "R_RESTART"), ("restart_failed", "R_RESTART_FAIL"))}
else:
    rec["counts"] = None  # stopped before the unit walk started: no unit touched
if env("R_ABORT"):
    rec["abort_reason"] = env("R_ABORT")
# DGN-1746: display names through update_surface's seat (the /update menu's
# product_name / member_label) -- no second name map here. Language: the
# ledger supervisor's (DOGANY_UPDATE_LANG), else the owning instance's notice
# language. A failure here never costs the record: each unit then falls back
# to its slug, never blank.
roots = {"restart_failures": env("R_RFAIL_ROOTS").split(),
         "bridges_left_down": env("R_DOWN_ROOTS").split(),
         "bridges_state_unconfirmed": env("R_SFAIL_ROOTS").split()}
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("dogany_update_surface", env("R_SURFACE"))
    surface = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(surface)
    lang = os.environ.get("DOGANY_UPDATE_LANG", "").strip()
    if not lang:
        lang_root = env("R_LANG_ROOT") or next((r for m in metas for _, r in m["members"] if r), "")
        lang = surface.notice_lang(lang_root, env("R_REPO")) if lang_root else "en"
    surface.annotate_record(rec, metas, roots, lang)
except Exception:
    for u in units:
        u.setdefault("product", u["unit"])
        u.setdefault("agents", [])
    for k, rl in roots.items():
        rec.setdefault(k + "_labels", list(rec[k]))
print(json.dumps(rec, ensure_ascii=False, indent=2))
PY
}

# _fallback_text <outcome> -- the minimal NEUTRAL owner push used only when
# no agent path exists. Plain facts the owner can act on (is my agent on?);
# no placeholder marker, no internal counter names.
_fallback_text() {
  local outcome="$1" down rfail sfail t
  case "$outcome" in
    done)    t="업데이트를 마쳤어요." ;;
    blocked) t="이번 업데이트는 적용되지 않았어요. 바뀐 것은 없어요." ;;
    partial) t="업데이트가 전부 적용되지는 않았어요." ;;
    *)       t="업데이트가 도중에 멈췄어요." ;;
  esac
  down="$(_names_of $(_left_down_roots))"
  rfail="$(_names_of ${RESTART_FAILED_ROOTS:-})"
  sfail="$(_names_of ${STOP_FAILED_ROOTS:-})"
  [[ -n "$down" ]]  && t="$t
꺼진 채로 남아 있는 에이전트: $down"
  [[ -n "$rfail" ]] && t="$t
다시 켜지지 않은 에이전트: $rfail"
  [[ -n "$sfail" ]] && t="$t
켜져 있는지 확인이 필요한 에이전트: $sfail"
  [[ "$outcome" != "done" ]] && t="$t
자세한 내용은 업데이트 실행 기록에 있어요."
  printf '%s' "$t"
}

# _agent_path_ok -- rc 0 when the owning instance can hear this run: it
# resolved, it carries a bridge (.telegram_bot/), and this run did not leave
# its bridge down or fail to bring it back (an inbox nobody polls is silence).
_agent_path_ok() {
  AGENT_PATH_WHY=""
  if [[ -z "$NOTIFY_ROOT" ]]; then
    AGENT_PATH_WHY="소유 인스턴스 미해석 (${NOTIFY_PUSH_WHY:-사유 없음})"; return 1
  fi
  if [[ ! -d "$NOTIFY_ROOT/.telegram_bot" ]]; then
    AGENT_PATH_WHY="소유 인스턴스에 .telegram_bot/ 없음 (브리지 없는 인스턴스)"; return 1
  fi
  case " $(_left_down_roots) ${RESTART_FAILED_ROOTS:-} " in
    *" $NOTIFY_ROOT "*)
      AGENT_PATH_WHY="이 실행이 소유 인스턴스 브리지를 내려둔 상태 (inbox 를 읽을 세션 없음)"; return 1 ;;
  esac
  return 0
}

# _inbox_drop <outcome> <json> -- DGN-217 writer contract (update.sh
# _relayout_inbox_drop / dispatch-detached write_session_inbox): temp name,
# atomic rename to *.md, UTF-8 via python. rc 0 = dropped.
_inbox_drop() {
  local outcome="$1" json="$2" spool="$NOTIFY_ROOT/.telegram_bot/session-inbox"
  mkdir -p "$spool" 2>/dev/null || return 1
  IB_DIR="$spool" IB_OUTCOME="$outcome" IB_JSON="$json" \
    "${PACK_PY3:-python3}" - <<'PY' || return 1
import os, time
def env(k):
    return os.environ.get(k, "").encode("utf-8", "replace").decode("utf-8", "replace")
d, outcome = env("IB_DIR"), env("IB_OUTCOME")
ts = time.strftime("%Y%m%d-%H%M%S")
name, n = f"update-apply-{outcome}-{ts}.md", 0
while os.path.exists(os.path.join(d, name)) or os.path.exists(os.path.join(d, "." + name + ".tmp")):
    n += 1
    name = f"update-apply-{outcome}-{ts}-{n}.md"
body = f"""[cron-inject] update run result (DGN-1709)

An update run just finished on this machine. The record below is the machine's
account of it -- it is for you, not for the owner.

The machine has already sent the rendered notice. If outcome is "done",
return NO_PUSH: write zero owner-facing text. Otherwise add at most ONE plain
reason line from the record detail in the owner's language, or NO_PUSH if
there is no useful reason to add. Never repeat the notice. No field names,
paths, ids, counters, or invented names. Use only the record's display fields:
"product", "agents", "bridges_left_down_labels", "restart_failures_labels"
and "bridges_state_unconfirmed_labels", copied as written.

outcome meanings: done = every unit updated and back on; partial = some units
did not update (see units); blocked = nothing was changed, every unit was
stopped by a safety check before any change; aborted = the run stopped midway
(counts, if present, are not final).

```json
{env("IB_JSON")}
```
"""
tmp = os.path.join(d, "." + name + ".tmp")
with open(tmp, "w", encoding="utf-8", errors="replace") as fh:
    fh.write(body)
os.replace(tmp, os.path.join(d, name))
print(os.path.join(d, name))
PY
}

# _deliver_result <label> <outcome> [abort-reason] -- ONE delivery per run.
# Machine-rendered notice first, then an optional reason-only inbox turn.
# Never a proxy sender; never aborts the update.
_deliver_result() {
  local label="$1" outcome="$2" abort_reason="${3:-}" json drop text
  NOTIFY_SENT=1
  if [[ -n "${RESULT_FILE_PRODUCT:-}" ]]; then
    json="$(_result_record "$outcome" "$abort_reason" 2>/dev/null || true)"
    if [[ -n "$json" ]] && printf '%s\n' "$json" > "$RESULT_FILE_PRODUCT.tmp.$$" \
        && mv -f "$RESULT_FILE_PRODUCT.tmp.$$" "$RESULT_FILE_PRODUCT"; then
      _log "${label} 결과 기록 1건 -> 업데이트 장부 감독자 ($RESULT_FILE_PRODUCT, outcome=$outcome) -- 재시작 확인 후 전달은 감독자 소유"
      return 0
    fi
    rm -f "$RESULT_FILE_PRODUCT.tmp.$$" 2>/dev/null || true
    _log "WARN: 장부 결과 파일 쓰기 실패 ($RESULT_FILE_PRODUCT) -- 기본 전달 경로로 대체 (조용한 실패 아님)"
  fi
  _resolve_notify_push
  json="$(_result_record "$outcome" "$abort_reason" 2>/dev/null || true)"
  # DGN-1757: machine owns the notice; the inbox may only add a reason.
  if [[ -n "$json" && -n "$NOTIFY_ROOT" ]] && printf '%s\n' "$json" | \
      "${PACK_PY3:-python3}" "${SCRIPT_DIR:-}/update_surface.py" result-send \
      --repo "$(cd "${SCRIPT_DIR:-.}/../.." && pwd)" --instance-root "$NOTIFY_ROOT"; then
    _log "${label} 렌더링 통지 1통 발송 -- 발신자 고정: $NOTIFY_ROOT/routines/push.sh (슬러그 '$NOTIFY_SLUG')"
    printf '%s\n' "$json" | sed 's/^/[apply]     /'
    if [[ "$outcome" != "done" ]] && _agent_path_ok; then
      _inbox_drop "$outcome" "$json" >/dev/null || _log "WARN: 결과 사유 inbox 쓰기 실패"
    fi
    return 0
  fi
  # A failed machine send must never become a model-written notice.
  if [[ -f "${SCRIPT_DIR:-}/update_surface.py" ]]; then
    _log "WARN: ${label} 렌더링 통지 실패 -- 수동 확인 필요 (outcome=$outcome)"
    printf '%s\n' "$json"
    return 0
  fi
  _agent_path_ok || true
  _log "${label}: 에이전트 경로 없음 -- ${AGENT_PATH_WHY:-결과 기록 생성 실패}. 중립 최소 통지로 대체 (outcome=$outcome)"
  [[ -n "$json" ]] && printf '%s\n' "$json" | sed 's/^/[apply]     /'
  text="$(_fallback_text "$outcome")"
  if [[ -z "$NOTIFY_PUSH" ]]; then
    _log "WARN: ${label} 통지 발송 불가 -- ${NOTIFY_PUSH_WHY}. 대리 발송 없음(발신자 고정 -- 도메인 에이전트는 인프라 보고 채널이 아님), 수동 통지 필요 (조용한 실패 아님). 본문:"
    printf '%s\n' "$text" | sed 's/^/[apply]     /'
    return 0
  fi
  if "$NOTIFY_PUSH" --text "$text" --audience owner >/dev/null 2>&1; then
    _log "${label} 통지 1통 발송 -- 발신자 고정: $NOTIFY_PUSH (슬러그 '$NOTIFY_SLUG')"
  else
    _log "WARN: ${label} 통지 발송 실패 (push rc != 0, $NOTIFY_PUSH) -- 수동 확인 필요. 본문:"
    printf '%s\n' "$text" | sed 's/^/[apply]     /'
  fi
  return 0
}

# _notify_abort <rc> -- ABORT-path single delivery (EXIT trap only). Fires
# ONLY when the run died before its normal completion line (RUN_COMPLETED=0)
# and nothing was delivered yet (NOTIFY_SENT=0). Runs AFTER the deferred-
# restart flush and the re-arm so bridges_left_down reflects what the trap
# already recovered (DGN-1506). Stop-verification failures (DGN-1511/1640)
# land in bridges_state_unconfirmed, never in bridges_left_down: liveness
# that was not re-confirmed here is never asserted either way. Never aborts,
# never exits (a failure inside the trap must not mask the original rc).
_notify_abort() {
  local xrc="$1" reason
  reason="${LAST_ABORT_MSG:-명시적 ABORT 메시지 없음 -- 커맨드 실패/신호로 중단 (rc=$xrc)}"
  if [[ -z "${N_TARGET+x}" ]]; then
    reason="$reason [적용 시작 전(계획/기계 검증) 단계에서 중단 -- 인스턴스 쓰기 0건]"
  fi
  _deliver_result "ABORT" "aborted" "$reason"
  return 0
}

# _mark_torndown <root> -- record that a loaded unit of this root was really
# removed from launchd by this run (DGN-1506 item 5). Separate from
# STOPPED_PENDING on purpose: _launchd_stop enters STOPPED_PENDING for every
# root it WALKS, including one whose labels were never loaded (bootout
# rc!=0). Re-arming that root would not be a re-arm -- it would be the first
# bootstrap the instance ever had. The two cases split exactly here.
_mark_torndown() {
  case " ${TORNDOWN_ROOTS:-} " in
    *" $1 "*) : ;;
    *) TORNDOWN_ROOTS="${TORNDOWN_ROOTS:+$TORNDOWN_ROOTS }$1" ;;
  esac
}

# _mark_stop_failed <root> -- record that at least one label under this
# root's stop VERIFICATION failed: a bootout ran (or was skipped as
# pointless) but that label is still alive in launchd after the bounded poll
# (DGN-1511 / DGN-1640). Tracked in its own list so it must never read as a
# stop (STOPPED_PENDING/_notify_abort) and must never be silently absorbed
# into a generic BLOCKED count (§6 accounting still counts it there too,
# unchanged -- this list is the human-facing breakdown of WHY).
#
# NOT mutually exclusive with TORNDOWN_ROOTS (DGN-1640): a root with several
# labels can have one that never verified gone alongside others that really
# left launchd -- the root is still owed those others back, so it can appear
# in both lists at once. This list alone means "at least one label of this
# root is still alive"; it says nothing about the OTHER labels.
N_STOP_FAIL=0
_mark_stop_failed() {
  case " ${STOP_FAILED_ROOTS:-} " in
    *" $1 "*) : ;;
    *) STOP_FAILED_ROOTS="${STOP_FAILED_ROOTS:+$STOP_FAILED_ROOTS }$1"
       N_STOP_FAIL=$((N_STOP_FAIL + 1)) ;;
  esac
}

# _unmark_stopped <root> -- drop every occurrence from STOPPED_PENDING
# (called on verified restart; a root can enter twice via fw + pack units).
_unmark_stopped() {
  local r new=""
  for r in ${STOPPED_PENDING:-}; do
    [[ "$r" == "$1" ]] && continue
    new="${new:+$new }$r"
  done
  STOPPED_PENDING="$new"
}

# Deferred-restart state (slice 4, spec §2 rule 1). Space-separated lists;
# roots contain no spaces (estate path doctrine, same assumption as the
# member "name:root" plan tokens).
DEFER_FW_ROOTS=""    # "name:root" of COMPLETED fw units awaiting loop-end restart
DEFER_UNIT_ROOTS=""  # "label:root" of COMPLETED pack/crew units whose root a LATER
                     # plan line stops again -- restart joins the flush (DGN-1710)
RESTARTED_ROOTS=""   # roots restarted by any completed unit this run
DOWN_ROOTS=""        # roots of INCOMPLETE units -- must stay down, never resurrected
TORNDOWN_ROOTS=""    # roots this run really removed loaded units from (re-arm input)
STOP_FAILED_ROOTS=""  # roots whose stop verification failed (bridge still alive, DGN-1511)
RESTART_FAILED_ROOTS="" # roots whose restart already failed once this run
FLUSH_DONE=0

TMP="$(mktemp -d "${TMPDIR:-/tmp}/dogany-apply.XXXXXX")" \
  || _abort "mktemp 실패 -- 사설 작업 디렉터리 없이는 진행하지 않습니다"

# EXIT trap: an ABORT anywhere after a completed fw unit must NOT lose the
# deferred restart (self-grill b: a lost deferral = a bridge that never
# comes back). The flush itself skips roots held down by an incomplete unit
# and roots already restarted -- abnormal exit gets the SAME policy.
_on_exit() {
  local xrc=$?
  if [[ "${FLUSH_DONE:-0}" -eq 0 && "${DRY:-0}" -eq 0 && -n "${DEFER_FW_ROOTS:-}${DEFER_UNIT_ROOTS:-}" ]]; then
    _log "비정상 종료 경로 -- 프레임워크 재시작 유예분을 유실하지 않도록 지금 flush합니다 (완주 단위만)"
    _flush_deferred || true
  fi
  # DGN-1506 item 5: the same rule as the flush, for the stop debt itself --
  # a run that died after a stop must not leave the instance with nothing
  # that can bring it back (its watchdog went out with the bridge). Ordered
  # BEFORE the abort notice so the notice reports what the trap recovered.
  if [[ "${RUN_COMPLETED:-0}" -eq 0 && "${DRY:-0}" -eq 0 && -n "${STOPPED_PENDING:-}" ]]; then
    if declare -F _rearm_stopped >/dev/null 2>&1; then
      _log "비정상 종료 경로 -- 이 실행이 정지시킨 인스턴스를 지금 재무장합니다 (미완주 인스턴스는 제외)"
      _rearm_stopped || true
    fi
  fi
  # ABORT-path notice (silent-death fix): a run that dies before its normal
  # completion line still owes the owner ONE notice -- gated on NOTIFY_SENT
  # so a run can never push twice, and on DRY (dry-run sends nothing).
  # Ordered AFTER the flush so the bridge-state line sees the recovery.
  if [[ "${RUN_COMPLETED:-0}" -eq 0 && "${NOTIFY_SENT:-0}" -eq 0 && "${DRY:-0}" -eq 0 ]]; then
    if declare -F _notify_abort >/dev/null 2>&1; then
      _notify_abort "$xrc" || true
    fi
  fi
  # DGN-1417: release the one-walker lock only after restarts and delivery.
  if declare -F _apply_lock_release >/dev/null 2>&1; then
    _apply_lock_release || true
  fi
  rm -rf "$TMP"
  exit "$xrc"
}
trap '_on_exit' EXIT INT TERM

# DGN-1417 owner decision 2026-09-27: /update is the only real apply door.
# Check before lock acquisition, planner execution or any instance stop/write.
# Keep the checked bytes private: never re-open a mutable plan for the walk.
LEDGER_PLAN_VERIFIED=0
if [[ "$DRY" -eq 0 && -z "$CAPTURE" ]]; then
  if [[ -n "$PLAN_FILE_PRODUCT" ]] && "$PACK_PY3" "$SCRIPT_DIR/update_surface.py" validate-apply \
      --plan "$PLAN_FILE_PRODUCT" --result "$RESULT_FILE_PRODUCT" \
      --repo "$(cd "$SCRIPT_DIR/../.." && pwd)" --supervisor "$PPID" > "$TMP/ledger-plan.tsv"; then
    LEDGER_PLAN_VERIFIED=1
  else
    LAST_ABORT_MSG="/update is the only real apply door. Raise an update offer and ask the owner to pick it in /update. Nothing was planned, stopped or changed by this run (DGN-1417)."
    # An unauthorized operator call has no owner delivery to complete.
    # Keep its aborted record local, even if no --result-file was supplied.
    NOTIFY_SENT=1
    _log "ABORT: $LAST_ABORT_MSG"
    _result_record aborted "$LAST_ABORT_MSG" > "$TMP/refused.json"
    if [[ -n "$RESULT_FILE_PRODUCT" ]]; then
      cp "$TMP/refused.json" "$RESULT_FILE_PRODUCT" || true
    fi
    cat "$TMP/refused.json"
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# 0) Machinery presence asserts (fail-closed -- absent machinery = ABORT).
# ---------------------------------------------------------------------------
[[ -f "$PLANNER"   && -r "$PLANNER"   ]] || _abort "플래너 부재 ($PLANNER) -- fail-closed"
[[ -f "$INSTALLER" && -r "$INSTALLER" ]] || _abort "pack_install 부재 ($INSTALLER) -- fail-closed"
[[ -f "$SEMVER_LIB" ]] || _abort "semver 비교기 부재 ($SEMVER_LIB) -- fail-closed"

# DGN-1456/1460: 설치 증거 판독기 -- 플래너와 설치기가 쓰는 것과 같은 lib.
# 적용 직전(변경 전 재판독)과 설치 직후(완주 집계 전) 두 번 소비한다.
INSTALL_EVIDENCE_LIB="$SCRIPT_DIR/lib/install_evidence.sh"
[[ -f "$INSTALL_EVIDENCE_LIB" && -r "$INSTALL_EVIDENCE_LIB" ]] \
  || _abort "설치 증거 판독기 부재 ($INSTALL_EVIDENCE_LIB) -- 장부 단독 판정 금지, fail-closed"
# shellcheck source=lib/install_evidence.sh
. "$INSTALL_EVIDENCE_LIB"
if [[ ! -f "$COMPAT_LINT" || ! -r "$COMPAT_LINT" ]]; then
  _abort "호환성 검사기(compat-lint.sh)가 없어 전체를 중단합니다 -- fail-closed (spec §4 belt 1). 프레임워크 설치가 불완전한 상태입니다. ($COMPAT_LINT)"
fi
if [[ "$DRY" -eq 0 ]]; then
  [[ -f "$MINT_RUN" && -r "$MINT_RUN" ]] || _abort "mint_run.sh 부재 ($MINT_RUN) -- 재시작 기계 없이 적용하지 않습니다, fail-closed"
fi

# ---------------------------------------------------------------------------
# 0b) One door, ONE walker at a time (DGN-1417 live-verify 2026-09-27).
# Observed: an agent's own update_apply.sh run (--plan-file, 5 units, started
# 17:29) and the owner's /update ledger run (two instances, tapped 17:31:31)
# walked the estate at the same time. Each run re-read the member the OTHER
# run had just landed, saw candidate == installed and reported it
# BLOCKED-STALE-PLAN "not changed ... before anything was touched" -- about a
# member that DID update inside the same minute. The door was single but let
# two walkers in. A real run now holds an estate-wide lock (a symlink whose
# target is "<pid>:<epoch>:<plan source>", created atomically by ln -s) from
# before the plan is read until the EXIT trap has flushed restarts and
# delivered the result. A second real run while the holder is alive is
# refused before any plan/stop/write; a lock whose holder pid is gone is
# reclaimed (compare-then-remove on the exact value read). --dry-run takes no
# lock (zero writes, and a preview must stay open during a real run).
# ---------------------------------------------------------------------------
APPLY_LOCK="${DOGANY_APPLY_LOCK:-$SHARED_HOME/update-apply.lock}"
APPLY_LOCK_TOKEN=""
APPLY_LOCK_HOLDER=""
_apply_lock_take() {
  local token held hpid n
  token="$$:$(date +%s):${PLAN_FILE_PRODUCT:-${PLAN_FILE_OVERRIDE:-planner}}"
  mkdir -p "$(dirname "$APPLY_LOCK")" 2>/dev/null || return 1
  for n in 1 2 3; do
    if ln -s "$token" "$APPLY_LOCK" 2>/dev/null; then
      APPLY_LOCK_TOKEN="$token"
      return 0
    fi
    held="$(readlink "$APPLY_LOCK" 2>/dev/null || true)"
    hpid="${held%%:*}"
    if [[ "$hpid" =~ ^[0-9]+$ ]] && kill -0 "$hpid" 2>/dev/null; then
      APPLY_LOCK_HOLDER="$held"
      return 1
    fi
    if [[ -n "$held" && "$(readlink "$APPLY_LOCK" 2>/dev/null || true)" == "$held" ]]; then
      _log "apply lock: holder '$held' is not alive -- stale lock reclaimed ($APPLY_LOCK)"
      rm -f "$APPLY_LOCK"
    elif [[ -z "$held" && -e "$APPLY_LOCK" && ! -L "$APPLY_LOCK" ]]; then
      APPLY_LOCK_HOLDER="(not a lock symlink: $APPLY_LOCK)"
      return 1
    fi
  done
  APPLY_LOCK_HOLDER="${held:-unknown}"
  return 1
}
_apply_lock_release() {
  [[ -n "${APPLY_LOCK_TOKEN:-}" ]] || return 0
  if [[ "$(readlink "$APPLY_LOCK" 2>/dev/null || true)" == "$APPLY_LOCK_TOKEN" ]]; then
    rm -f "$APPLY_LOCK"
  fi
  APPLY_LOCK_TOKEN=""
  return 0
}
if [[ "$DRY" -eq 0 ]]; then
  if ! _apply_lock_take; then
    _abort "another update is already applying on this host (lock $APPLY_LOCK held by '${APPLY_LOCK_HOLDER:-unknown}'). One apply door, one run at a time: nothing was planned, stopped or changed by this run. Retry after that run has delivered its result (DGN-1417)"
  fi
fi

# ---------------------------------------------------------------------------
# 0c) DGN-1786: never bring back an agent that is being retired.
# Observed 2026-09-28: a plan line still listed an agent whose units had been
# stopped on purpose for its retirement; this walker re-bootstrapped all of
# them and restarted its bot, and the retirement ended retire.partial. A
# retirement now marks the agent (scripts/pack/lib/retire_marker.py) BEFORE it
# looks for this run's lock, and this run reads the marks of a unit's agents
# AFTER taking the lock and BEFORE stopping anything -- so one of the two
# always sees the other. A marked agent's unit is not attempted at all: no
# stop, no write, no restart (the agent stays exactly as the retirement left
# it), counted under blocked with the reason below. The check runs in
# --dry-run too, so the preview says the same thing.
# ---------------------------------------------------------------------------
RETIRE_MARKER_LIB="$SCRIPT_DIR/lib/retire_marker.py"
[[ -f "$RETIRE_MARKER_LIB" && -r "$RETIRE_MARKER_LIB" ]] \
  || _abort "retire marker reader missing ($RETIRE_MARKER_LIB) -- cannot tell which agents are being retired, fail-closed (DGN-1786)"
# Plain reason for the result record (ASCII, operator lane). The owner-facing
# sentence is gates.json update.retiring user_hint_ko (owner-confirmed,
# dec-233), carried by the gate marker _retiring_hold writes below.
RETIRING_REASON="not updated: this agent is being deleted, so it was left switched off (owner copy: gates.json update.retiring, dec-233)"
# _retiring_hold <unit label> <name:root | root>... -- rc 0 (and logs the
# BLOCKED line) when any of the agents is being retired; rc 1 when none is.
# An unreadable verdict holds the unit (fail-closed). The hold is a PLANNED
# gate: the BLOCKED line goes through _gate_stop update.retiring so a wrapper
# that set DOGANY_GATE_STATE_FILE sends the registered owner copy, and C2 of
# scripts/user-gate-lint.py sees the registry entry's real call site.
_retiring_hold() {
  local unit="$1" e root out rc held=""
  shift
  for e in "$@"; do
    root="${e#*:}"
    [[ -n "$root" ]] || continue
    rc=0
    out="$("$PACK_PY3" "$RETIRE_MARKER_LIB" check "$root" 2>&1)" || rc=$?
    case "$rc" in
      0) held="${held:+$held, }$(printf '%s' "$out" | cut -f2) (receipt: $(printf '%s' "$out" | cut -f3))" ;;
      1) : ;;
      *) held="${held:+$held, }$root (retire marker unreadable: $out)" ;;
    esac
  done
  [[ -n "$held" ]] || return 1
  _gate_stop update.retiring "  $unit: BLOCKED -- agent being retired: $held. Not attempted: no stop, no write, no restart (DGN-1786)" || :
  return 0
}

# _vercmp <version> <range> -- rc 0 satisfied / 1 not / 2 unevaluable
_vercmp() {
  local rc=0
  "$PACK_PY3" "$SEMVER_LIB" satisfies "$1" "$2" >/dev/null 2>&1 || rc=$?
  return "$rc"
}

_reg_repo() { # <id> -- prints registry repo path ('' when absent)
  [[ -f "$SOURCES_CONF" ]] || { echo ""; return 0; }
  sed -n "s|^$1\.repo=||p" "$SOURCES_CONF" | head -n1
}

_installed_ver() { # <conf> <pack-id> -- prints installed version ('' = none)
  local packs it
  packs="$(_conf_get "$1" DOGANY_PACKS)"
  [[ -n "$packs" ]] || { echo ""; return 0; }
  IFS=',' read -r -a _items <<< "$packs"
  for it in "${_items[@]}"; do
    if [[ "${it%%@*}" == "$2" ]]; then echo "${it#*@}"; return 0; fi
  done
  echo ""
}

# _install_gate_passed <installer-output-file> -- DGN-1682 belt 2.
# The PRIMARY success judgment is machine-checkable and lives at the call
# sites: pack_install rc (DOGANY_STRICT_GATES=1 turns a skipped compat-lint
# gate into a nonzero exit inside the installer) + the DOGANY_PACKS record +
# the delivery receipt. This scan is the defense-in-depth second belt only.
# It used to grep ONE legacy wording ("compat-lint: install-side gate PASS",
# printed only by the legacy agent/module path), so every landed kind=pack
# upgrade -- whose contract path prints "pack: compat-lint PASS" -- was judged
# INCOMPLETE and left its bridge down (rehearsal 2). Accept the line EACH
# install path actually prints (legacy, kind=pack, kind=kit), anchored to the
# installer's OWN top-level log line (optional "[ts] " prefix) so a nested
# "[autoresolve:<kit>]" line from a requires_kit sub-install can never stand
# in for the outer pack's own gate.
_install_gate_passed() {
  grep -Eq '^(\[[^]]*\] )?(compat-lint: install-side gate PASS|(pack|kit): compat-lint PASS)$' "$1"
}

# ---------------------------------------------------------------------------
# 1) Produce the plan (the planner's product IS the plan -- no re-derivation).
# ---------------------------------------------------------------------------
PLAN_TSV="$TMP/plan.tsv"
if [[ -n "$PLAN_FILE_PRODUCT" ]]; then
  # DGN-1417: say "ledger-selected" only when the file IS a /update ledger's
  # selected plan (<ledger>.json.plan next to its ledger). The 09-27 run
  # logged this line for a hand-made plan file, which hid the second walker.
  if [[ "$LEDGER_PLAN_VERIFIED" -eq 1 ]]; then
    _log "ledger-selected plan received ($PLAN_FILE_PRODUCT)"
  else
    _log "operator plan file received, not a /update ledger selection ($PLAN_FILE_PRODUCT)"
  fi
  if [[ "$LEDGER_PLAN_VERIFIED" -eq 1 ]]; then
    cp "$TMP/ledger-plan.tsv" "$PLAN_TSV"
  else
    cp "$PLAN_FILE_PRODUCT" "$PLAN_TSV" || _abort "ledger plan unreadable"
  fi
elif [[ -n "$PLAN_FILE_OVERRIDE" ]]; then
  _log "TEST: DOGANY_APPLY_PLAN_FILE 주입 -- 플래너 실행 생략 ($PLAN_FILE_OVERRIDE)"
  cp "$PLAN_FILE_OVERRIDE" "$PLAN_TSV" || _abort "주입 plan 파일 판독 불가"
else
  _log "== 계획 산출 (update_plan.sh 소비) =="
  PLAN_RC=0
  # DGN-1818: the planner tag-fetches public-install pack clones (its step
  # 1b); a dry-run keeps its zero-write contract and plans on local tags.
  _plan_no_fetch="${DOGANY_PLAN_NO_FETCH:-0}"
  [[ "$DRY" -eq 1 ]] && _plan_no_fetch=1
  DOGANY_PLAN_NO_FETCH="$_plan_no_fetch" DOGANY_PLAN_EMIT="$PLAN_TSV" bash "$PLANNER" || PLAN_RC=$?
  if [[ "$PLAN_RC" -ne 0 ]]; then
    _abort "플래너 rc=$PLAN_RC -- 계획 실패 상태에서는 어떤 적용도 하지 않습니다 (fail-closed)"
  fi
fi
[[ -f "$PLAN_TSV" ]] || _abort "plan emit 파일 미생성 ($PLAN_TSV) -- fail-closed"
head -n1 "$PLAN_TSV" | grep -q '^PLAN-EMIT-V1$' \
  || _abort "plan emit 헤더 불일치 -- 이 실행의 산출물이 아님, fail-closed"

# Item count: FW / PACK / ATOMIC headers only -- the structured ATOMIC-FW /
# ATOMIC-PACK / ATOMIC-END member lines belong to their group, not the count.
PLAN_ITEMS="$(awk -F'\t' '$1=="FW" || $1=="PACK" || $1=="ATOMIC" {n++} END {print n+0}' "$PLAN_TSV")"
_log ""
_log "== 적용 단계 (통합 1턴: framework + pack) dry-run=$DRY =="
_log "계획 적용 항목 ${PLAN_ITEMS}건"
if [[ "$PLAN_ITEMS" -eq 0 ]]; then
  _log "계획 적용 항목 0건 -- 차단/보류 사유는 상단 플래너 출력 참조 (0건은 통과가 아니라 의심)"
fi

# Root-touch index (DGN-1710): "<plan line no>\t<root>" for every root a plan
# line can stop. A completed unit whose root a LATER line will stop again
# defers its restart to the loop-end flush instead of restarting now, so the
# agent restarts exactly once per run whatever the plan order (DGN-1417 put
# framework LAST: without this, the pack/kit unit restarted, the fw unit
# stopped the same bridge again, and the flush dedup skipped it -> the re-arm
# pass restarted it a SECOND time). Line numbers match PLAN_LINE_N below.
PLAN_TOUCH="$TMP/plan.touch"
awk -F'\t' '
  $1=="FW" || $1=="ATOMIC-FW" { if ($3 != "") print NR "\t" $3; next }
  $1=="PACK" || $1=="ATOMIC-PACK" {
    n = split($8, m, " ")
    for (i = 1; i <= n; i++) { r = m[i]; sub(/^[^:]*:/, "", r); if (r != "") print NR "\t" r }
  }' "$PLAN_TSV" > "$PLAN_TOUCH" || _abort "plan root-touch 색인 생성 실패 -- fail-closed"
PLAN_LINE_N=0

# _touched_later <root> -- rc 0 when a plan line AFTER the current one
# (PLAN_LINE_N) can stop <root> again.
_touched_later() {
  awk -F'\t' -v n="$PLAN_LINE_N" -v r="$1" '$1+0 > n+0 && $2 == r {f=1} END {exit !f}' "$PLAN_TOUCH"
}

# ---------------------------------------------------------------------------
# 2) Unit walk. Counts per §6 -- every lane printed, 0 is suspicion.
# ---------------------------------------------------------------------------
N_TARGET=0      # applicable atomic units (instance-scoped pack x instance)
N_OK=0          # completed + record-verified
N_BLOCK=0       # blocked before any write (bridge untouched)
N_INCOMPLETE=0  # started but did not complete -- bridge stays DOWN
N_UNTRIED=0     # not attempted after a fail-fast stop
N_SKIP=0        # out-of-scope plan items (fw / crew / atomic), loud
N_RESTART=0     # bridges restarted
N_RESTART_FAIL=0
FAILFAST=0
UNIT_N=0
OVERALL_RC=0
# DGN-1682 item 3: kits whose crew unit (or ATOMIC group) came back BLOCKED
# in this run. A later pack unit whose candidate manifest names one of them
# in requires_kit.kit is BLOCKED-BY-DEPENDENCY before its bridge is touched
# -- it cannot install over a kit that did not move, and stopping its bridge
# only to fail the requires_kit gate left the bot off (rehearsal 2 S3).
BLOCKED_KITS=""
N_BLOCK_DEP=0
BLOCK_DEP_UNITS=""

# _label_live <label> -- rc 0 only when the label is STILL registered AND
# its listed PID is live. `launchctl list` can retain an exiting job briefly;
# its row alone is therefore not proof that the bridge is alive. Read the
# registration/PID pair, test that PID, then read the pair AGAIN. The final
# read wins: a vanished label, a missing/non-numeric PID, or a dead PID at
# the end of the verification window is a verified stop, never "alive".
_label_live() {
  local label="$1" pid final_pid
  pid="$(launchctl list 2>/dev/null | awk -v l="$label" '$3==l {print $1; exit}')"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  final_pid="$(launchctl list 2>/dev/null | awk -v l="$label" '$3==l {print $1; exit}')"
  [[ "$final_pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$final_pid" 2>/dev/null || return 1
  return 0
}

# _label_gone <label> -- bounded poll: rc 0 once the label is no longer
# live, rc 1 only when it is registered with a live PID on the FINAL read
# after STOP_POLL_TRIES (DGN-1640). `launchctl bootout` returning 0 means
# ACCEPTED, not FINISHED. Polling short intervals up to a bound returns as
# soon as teardown finishes; the final registration+PID read prevents an
# exiting bridge from being reported alive on a stale list row.
_label_gone() {
  local label="$1" i=0
  while :; do
    if ! _label_live "$label"; then
      return 0
    fi
    i=$((i + 1))
    [[ "$i" -ge "$STOP_POLL_TRIES" ]] && return 1
    sleep "$STOP_POLL_INTERVAL"
  done
}

# _stop_order <root> -- prints the root's unit plists, one per line, in the
# fixed stop order (DGN-1739): bridge/*.watchdog.plist, then the remaining
# bridge/*.plist, then routines/*.plist; each group sorted (LC_ALL=C) so the
# order never depends on locale or on what the glob happens to return.
# A watchdog unit is named <prefix>.watchdog.plist (watchdog_setup.sh
# excludes exactly that suffix when it picks the bridge plist).
_stop_order() {
  local root="$1" p
  for p in "$root"/bridge/*.watchdog.plist; do
    if [[ -e "$p" ]]; then printf '%s\n' "$p"; fi
  done | LC_ALL=C sort
  for p in "$root"/bridge/*.plist; do
    [[ -e "$p" ]] || continue
    case "$p" in *.watchdog.plist) continue ;; esac
    printf '%s\n' "$p"
  done | LC_ALL=C sort
  for p in "$root"/routines/*.plist; do
    if [[ -e "$p" ]]; then printf '%s\n' "$p"; fi
  done | LC_ALL=C sort
}

# _launchd_stop <root> -- bootout every bridge/routines plist label (the
# routines/watchdog units are included so nothing can resurrect the bridge
# mid-apply). rc 0 only when every label is VERIFIED gone afterwards -- a
# bootout failure with the job still alive must never read as "already
# stopped" (files would be swapped under a RUNNING bridge, self-grill a).
#
# Taking the watchdog out also takes out the instance's OWN recovery arm
# (bridge/watchdog.sh DGN-888: enable + bootstrap the vanished bridge label),
# so from here the instance cannot come back on its own. That debt is not
# the owner's to pay by hand: every root marked torn down below is re-armed
# by _rearm_stopped at loop end / on the EXIT trap (DGN-1506 item 5).
#
# ORDER (DGN-1739): watchdog units FIRST, then the other bridge units, then
# routines. Glob order put <name>.newbridge.plist before <name>.watchdog.plist,
# so the bridge left launchd while its watchdog was still loaded; the
# watchdog's DGN-888 arm saw the vanished label and bootstrapped it again
# within a second, and the stop verification below failed (2026-09-27 crew
# kit apply BLOCKED twice). With the recoverer gone first there is nothing
# left to race. _stop_order is the single source of this order.
_launchd_stop() {
  local root="$1" p label rc=0 tore=0 label_booted=0 order
  # Abort-notice bookkeeping: from this point the root counts as "stopped
  # and not yet restarted" until a verified restart unmarks it.
  STOPPED_PENDING="${STOPPED_PENDING:+$STOPPED_PENDING }$root"
  # Checked substitution: an ordering failure must never read as "no units,
  # nothing to stop" (files would be swapped under a running bridge).
  if ! order="$(_stop_order "$root")"; then
    _log "    unit stop order unreadable under $root (refusing to treat root as stopped)"
    _mark_stop_failed "$root"
    return 1
  fi
  # fd 3, not stdin: nothing in the loop body may eat the list.
  while IFS= read -r p <&3; do
    [[ -n "$p" && -e "$p" ]] || continue
    if ! label="$(plist_label "$p")"; then
      _log "    launchd Label unreadable: $p (refusing to treat root as stopped)"
      rc=1
      continue
    fi
    if [[ -n "$CAPTURE" ]]; then
      printf 'launchctl bootout gui/UID/%s\n' "$label" >> "$CAPTURE"
      _log "    (rehearsal) bootout 캡처: $label"
      # Rehearsal stands in for a real bootout, so the root counts as torn
      # down here too -- otherwise the capture would show the stop without
      # the re-arm that a real run owes for it.
      tore=1
      continue
    fi
    label_booted=0
    if launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1; then
      _log "    브리지 정지: $label"
      label_booted=1
    else
      _log "    브리지 정지: $label (bootout rc!=0 -- 미적재 추정, 검증으로 확인)"
    fi
    # verification: the label must be GONE from launchd now (self_restart.sh
    # cur_pid precedent), given a bounded window to actually finish exiting.
    # Alive label past the bound = stop failure = unit must not proceed.
    if _label_gone "$label"; then
      # A label only really left launchd if BOTH its bootout was accepted
      # AND this specific label verified gone (DGN-1640): bootout rc 0 alone
      # is ACCEPTED, not FINISHED, and is not enough to owe the root a
      # re-arm on its own -- see _label_gone's header.
      [[ "$label_booted" -eq 1 ]] && tore=1
    else
      _log "    정지 검증 FAIL: $label 이 launchd에 살아 있음 (바운드 폴 만료) -- 이 단위를 진행하지 않습니다 (fail-closed)"
      rc=1
    fi
  done 3<<< "$order"
  # `tore` and `rc` are tracked PER LABEL above and only OR'd together here,
  # so they are independent facts, not one verdict (DGN-1640): a root can
  # have BOTH really lost a unit (tore=1) AND have a different label whose
  # verification failed (rc=1). The old code required tore=1 AND rc=0 to
  # credit the root with anything, so one bad label voided the root's claim
  # on every OTHER label that really did go down. A root that really lost
  # units is owed them back regardless of one label's verdict; the
  # stop-failed label is reported on its own via _mark_stop_failed either way.
  if [[ "$tore" -eq 1 ]]; then
    _mark_torndown "$root"
  fi
  if [[ "$rc" -eq 1 ]]; then
    # DGN-1511: at least one label's stop verification failed -- reported so
    # the abort notice can name it. STOPPED_PENDING already holds this root
    # (entered unconditionally at walk time, above) but that list means
    # "attempted", not "verified" -- callers that need the verified facts
    # (the abort notice, any future re-arm-style reader) must consult
    # TORNDOWN_ROOTS / STOP_FAILED_ROOTS instead of trusting STOPPED_PENDING
    # alone. The two lists are no longer mutually exclusive (see above).
    _mark_stop_failed "$root"
  fi
  return "$rc"
}

_plist_count() { # <root>
  local root="$1" n=0 p
  for p in "$root"/bridge/*.plist "$root"/routines/*.plist; do
    [[ -e "$p" ]] && n=$((n + 1))
  done
  echo "$n"
}

_restart() { # <root> -- rc 0 on success (successful roots recorded for the
  local root="$1" rc=0    # loop-end deferral dedup: one restart per agent)
  if [[ -n "$CAPTURE" ]]; then
    printf 'mint_run.sh start --root %s\n' "$root" >> "$CAPTURE"
    _log "    (rehearsal) 재시작 캡처: mint_run.sh start --root $root"
  else
    bash "$MINT_RUN" start --root "$root" < /dev/null || rc=$?
  fi
  if [[ "$rc" -eq 0 ]]; then
    RESTARTED_ROOTS="${RESTARTED_ROOTS:+$RESTARTED_ROOTS }$root"
    _unmark_stopped "$root"
  else
    # One attempt per root per run. Recorded here (the single place that
    # knows) so the loop-end / trap re-arm does not immediately repeat an
    # attempt that just failed: an instant retry would only count the same
    # failure twice, and the root is already named to the owner by the
    # caller's log line and by the abort notice.
    RESTART_FAILED_ROOTS="${RESTART_FAILED_ROOTS:+$RESTART_FAILED_ROOTS }$root"
  fi
  return "$rc"
}

_mark_down() { # <root>... -- record roots of an INCOMPLETE unit (stay down)
  local r
  for r in "$@"; do
    DOWN_ROOTS="${DOWN_ROOTS:+$DOWN_ROOTS }$r"
  done
}

# _flush_deferred -- loop-end flush of deferred restarts (spec §2: fw restart
# deferred to loop end, folded into the atomic-unit policy). Two inputs:
# DEFER_FW_ROOTS (every completed fw unit) and DEFER_UNIT_ROOTS (a completed
# pack/crew unit whose root a LATER plan line stops again -- DGN-1710, the
# framework-last order of DGN-1417). Per entry:
#   - root held down by a later INCOMPLETE unit  -> stays down (loud)
#   - root listed as a member of an on-disk crew lease whose state is DIRTY
#     or INCOMPLETE -> stays down (loud, DGN-1509 G4 -- see below)
#   - root already attempted earlier in this same flush -> skip (one restart
#     per agent)
#   - root restarted by a later completed unit and NOT stopped since -> skip
#     (fw-first order: the pack/kit unit after the fw unit restarted it)
#   - otherwise -> restart now, counted
# The two stay-down arms are NOT fw-first-only any more (DGN-1710 decision:
# kept): a deferred pack/crew restart meets them in the fresh fw-last order
# whenever the later fw unit on that root is INCOMPLETE (DOWN_ROOTS) or a
# later crew transaction on it dies mid-flight (lease). They also still
# guard --plan-file plans recorded in the old fw-first order.
# Runs on the normal path AND from the EXIT trap (abnormal exits keep their
# deferrals -- self-grill b). Idempotent via FLUSH_DONE + list reset.
#
# DGN-1509 G4: this is B2's SECOND arm. _on_exit calls this function BEFORE
# _rearm_stopped (trap order below) -- a signal that lands while a LATER
# crew-atomic unit is mid-transaction (T1..TN, after an EARLIER fw unit in
# the same run already completed and deferred its restart here) used to
# reach this loop with DOWN_ROOTS still empty (the crew unit dies before
# _mark_down ever runs -- update_apply.sh _mark_down call sites, well after
# the crew's T1..TN loop) and RESTARTED_ROOTS not yet containing the fw
# root either, so the deferred fw restart fired on top of a possibly
# half-migrated shared DB -- the exact bug _rearm_stopped's on-disk lease
# read (below) already closed for ITS OWN restart path. The fw-first
# sequential plan that makes an fw root ALSO a crew member reachable here is
# real (update_plan.sh:1039-1053 "fw items ... -> kit packs" default order).
# Same fix, same on-disk read, same reasoning as _rearm_stopped -- see that
# function's header for the full write-before-first-mutation argument;
# repeating it here would just be two copies to keep in sync.
_flush_deferred() {
  FLUSH_DONE=1
  [[ -n "${DEFER_FW_ROOTS:-}${DEFER_UNIT_ROOTS:-}" ]] || return 0
  local e name root label who="" tried=""
  # Computed ONCE per pass, same reasoning as _rearm_stopped's copy: a scan
  # inside the loop would be wasteful and could observe a different answer
  # mid-loop if a lease transitions between two roots' checks.
  local lease_dirty_roots
  lease_dirty_roots="$(_crew_lease_dirty_member_roots "$$" "$CREW_LEASE_RUN_ID")"
  _log ""
  _log "== 재시작 유예분 flush (루프 끝 -- 완주 단위만, 에이전트당 재시작 1회) =="
  # Pack/crew deferrals (DGN-1710) first, then framework ones. Every entry
  # passes the same stay-down checks; a root is attempted at most ONCE here.
  for e in ${DEFER_UNIT_ROOTS:-} -- ${DEFER_FW_ROOTS:-}; do
    if [[ "$e" == "--" ]]; then who="framework"; continue; fi
    name="${e%%:*}"
    root="${e#*:}"
    if [[ "${who:-}" == "framework" ]]; then label="framework $name"; else label="$name"; fi
    if [[ " $DOWN_ROOTS " == *" $root "* ]]; then
      _log "  $label: 재시작 보류 -- 동일 인스턴스의 후속 원자 단위 미완주로 정지 유지 (반쪽 상태 라이브 복귀 금지)"
      continue
    fi
    if [[ -n "$lease_dirty_roots" && " $lease_dirty_roots " == *" $root "* ]]; then
      _log "  $label: 재시작 보류 -- 크루 lease가 DIRTY/INCOMPLETE (디스크 기록, DGN-1509 G4: 신호 중단으로 DOWN_ROOTS가 비어 있어도 반쯤 마이그레이션된 공유 DB 위로 재시작하지 않습니다. 해제는 --clear-incomplete 로만)"
      continue
    fi
    if [[ " $tried " == *" $root "* ]]; then
      _log "  $label: 같은 인스턴스를 이 flush 에서 이미 재시작 처리함 -- 중복 재시작 생략 (에이전트당 재시작 1회)"
      continue
    fi
    # Up = restarted by a completed unit AND not stopped again since
    # (_restart unmarks STOPPED_PENDING, _launchd_stop re-marks it). The
    # STOPPED_PENDING half is the DGN-1710 belt: a restart that an EARLIER
    # unit did before a later stop no longer counts as "already restarted".
    if [[ " $RESTARTED_ROOTS " == *" $root "* && " ${STOPPED_PENDING:-} " != *" $root "* ]]; then
      _log "  $label: 후속 완주 단위가 이미 재시작함 -- 중복 재시작 생략 (에이전트당 재시작 1회)"
      continue
    fi
    tried="$tried $root"
    if _restart "$root"; then
      N_RESTART=$((N_RESTART + 1))
      _log "  $label: 브리지 재시작 완료 (유예분)"
    else
      N_RESTART_FAIL=$((N_RESTART_FAIL + 1))
      OVERALL_RC=2
      _log "  $label: 재시작 실패 -- 수동 재기동 필요: $MINT_RUN start --root $root"
    fi
  done
  DEFER_UNIT_ROOTS=""
  DEFER_FW_ROOTS=""
}

# _rearm_stopped -- machine re-arm of every root this run took units away from
# and never gave them back (DGN-1506 item 5). Runs at loop end AND from the
# EXIT trap, so an ABORT/INT/TERM anywhere after a stop still pays the debt.
#
# WHY IT MUST EXIST. _launchd_stop boots out $root/bridge/*.plist -- and the
# watchdog unit lives in bridge/, so the DGN-888 vanished-label recovery arm
# (bridge/watchdog.sh: enable + bootstrap the marker plist) is INSIDE the set
# being torn down. The instance's own recoverer is therefore not running in
# exactly the state it exists for, and every pre-existing exit from that state
# was a log line telling a person to run mint_run.sh start. Reproduced
# 2026-09-16 on throwaway labels (com.dogany.dsp1506probe.*, never a live
# unit): after bootout the label is gone from launchd (print rc=113), it is
# NOT in print-disabled and carries no suppression marker, no further
# StartInterval fire happens, and a re-bootstrap re-arms it in the same second
# via RunAtLoad. So the unit is held down by the bootout alone -- and the only
# actor that knows it did the bootout is this run.
#
# WHAT IT REFUSES TO DO:
#   - DOWN_ROOTS (an INCOMPLETE unit's root): held down deliberately, because
#     re-arming the watchdog there would let the DGN-888 arm bring a
#     half-swapped bridge back to live. Stay-down wins; that root belongs to
#     the forward-only re-run / teardown lane, not to this function.
#   - DGN-1509 B2/G4: a root listed as a member of an on-disk crew lease
#     acquired by THIS run, whose state is DIRTY or INCOMPLETE. An older
#     lease which refused acquisition does not mean this run wrote state.
#     This is a SECOND, DISK-BACKED stay-down input alongside DOWN_ROOTS --
#     it exists because DOWN_ROOTS is an in-PROCESS variable the caller only
#     populates AFTER _apply_crew returns (update_apply.sh _mark_down call
#     sites, well after the T1..TN install loop). A signal that kills this
#     process WHILE _apply_crew is still running (mid-migration) never
#     reaches that assignment, so DOWN_ROOTS alone would stay empty and
#     every member bridge would be re-armed on top of a possibly
#     half-migrated shared DB. The lease is written pessimistically (DIRTY,
#     fsync'd) BEFORE _apply_crew touches anything live, so it is already
#     on disk by the time any signal lands -- this trap-path read closes
#     exactly that window (see lib/crew_lease.sh header for the full B2
#     writeup). Empty/absent lease (today's live estate, always -- nothing
#     produces one outside a crew transaction in flight) makes this check a
#     no-op, byte-identical to before this fix. _on_exit calls THIS
#     function SECOND -- _flush_deferred (called first, above) reads the
#     SAME lease for the SAME reason, because a completed fw unit from
#     EARLIER in the same run can defer a restart for a root that is also a
#     member of a crew transaction the signal caught mid-flight LATER in
#     the run (G4 -- see that function's header). Two call sites, one
#     read -- both must be consulted before this trap can restart anything.
#   - a root nothing was actually taken down from and whose stop verification
#     did not fail: its units were never loaded -- nothing to re-load, and a
#     first bootstrap would be a mint decision, not a recovery. A
#     STOP_FAILED root is intentionally different: it remains recovery debt
#     because an exit can win the race immediately after verification.
#
# THE OTHER HALF OF DGN-1506 item 5 -- the shape where units were NEVER
# registered (mint stopped after the token but before unit registration;
# measured 2026-09-16 on dogany-health: 0 plists, 0 launchd entries, .env
# with a live token) -- does not belong to this function and structurally
# cannot reach it: every apply path fail-closes on "재시작 대상 plist 0건"
# BEFORE it stops anything (_fw_precheck / _apply_unit / _apply_crew T0), so
# such a root never enters STOPPED_PENDING. There is nothing to re-arm there
# because nothing was ever armed; that state's owner is the teardown lane
# (DGN-1506 item 6), not a restart.
# A failed restart leaves the root in STOPPED_PENDING so the abort notice
# still names it: a machine attempt that failed is reported, never silent.
_rearm_stopped() {
  [[ "${DRY:-0}" -eq 0 ]] || return 0
  [[ -n "${STOPPED_PENDING:-}" ]] || return 0
  local r
  # DGN-1509 B2: computed ONCE per pass (not per-root) -- a filesystem scan
  # inside the loop would be wasteful and, worse, could observe a DIFFERENT
  # answer mid-loop if a lease transitions between two roots' checks.
  local lease_dirty_roots
  lease_dirty_roots="$(_crew_lease_dirty_member_roots "$$" "$CREW_LEASE_RUN_ID")"
  REARM_HEAD=0
  for r in ${STOPPED_PENDING}; do
    if [[ " ${DOWN_ROOTS:-} " == *" $r "* ]]; then
      _rearm_head
      _log "  재무장 보류: $r -- 미완주 단위의 인스턴스 (반쪽 상태 라이브 복귀 금지, 정지 유지)"
      continue
    fi
    if [[ -n "$lease_dirty_roots" && " $lease_dirty_roots " == *" $r "* ]]; then
      _rearm_head
      _log "  재무장 보류: $r -- 크루 lease가 DIRTY/INCOMPLETE (디스크 기록, DGN-1509 B2: 신호 중단으로 DOWN_ROOTS가 비어 있어도 이 실행이 내린 단위를 재무장하지 않습니다. 해제는 --clear-incomplete 로만)"
      continue
    fi
    # A STOP_FAILED root is recovery debt too. The failure says only that
    # verification could not prove the stop; it does NOT prove this run left
    # every pre-existing unit running. In particular, an exiting bridge can
    # disappear between the final verification read and this loop. Re-arm it
    # unless a deliberate stay-down rule above applies, so this run never
    # strands a unit it found running before apply.
    if [[ " ${TORNDOWN_ROOTS:-} " != *" $r "* \
          && " ${STOP_FAILED_ROOTS:-} " != *" $r "* ]]; then
      _rearm_head
      _log "  재무장 대상 아님: $r -- 이 실행이 실제로 내린 유닛 없음 (정지 시점에 이미 미적재)"
      continue
    fi
    if [[ " ${RESTART_FAILED_ROOTS:-} " == *" $r "* ]]; then
      _rearm_head
      _log "  재무장 생략: $r -- 이 실행에서 이미 재시작을 시도했고 실패함 (위 로그 참조, 실패는 이미 집계됨)"
      continue
    fi
    _rearm_head
    if _restart "$r"; then
      N_RESTART=$((${N_RESTART:-0} + 1))
      _log "  재무장 완료: $r (이 실행이 내린 유닛을 이 실행이 되돌림)"
    else
      N_RESTART_FAIL=$((${N_RESTART_FAIL:-0} + 1))
      OVERALL_RC=2
      _log "  재무장 실패: $r -- 수동 재기동 필요: $MINT_RUN start --root $r"
    fi
  done
  return 0
}

# _rearm_head -- print the section header once per _rearm_stopped pass.
REARM_HEAD=0
_rearm_head() {
  [[ "$REARM_HEAD" -eq 0 ]] || return 0
  REARM_HEAD=1
  _log ""
  _log "== 정지분 재무장 (이 실행이 정지시킨 뒤 재시작에 도달 못 한 인스턴스) =="
}

# ---------------------------------------------------------------------------
# Framework unit machinery (slice 4). Application is delegated ENTIRELY to
# the instance's own routines/self-update.sh (the shipped framework loop:
# channel/pin resolution, fetch, git-archive extraction, child update.sh
# --no-pull --yes, and the fail-closed fw_reqframework_guard inside
# update.sh). The loop adds ONLY: fresh pre-checks, the stop envelope, the
# planned-candidate pin, landing verification, and restart deferral via
# self-update.sh's OWN --no-restart flag (no self-update modification).
# ---------------------------------------------------------------------------

# _fw_precheck <name> <root> <to> -- read-only; rc 0 ok / 1 blocked (logged)
# / 3 already landed by an earlier unit of THIS run (nothing left to do).
# Sets FW_SU (resolved self-update entrypoint), FW_NOW (fresh version) and,
# on a blocked stale plan, FW_BLOCK_REASON (plain reason for the record).
FW_SU=""
FW_NOW=""
FW_BLOCK_REASON=""
# DGN-1417: "<root>=<version>" of every framework landing this run verified
# (_fw_payload). A later unit that finds candidate == installed on one of
# these roots was satisfied by this same run -- it is not a stale plan and
# must never be reported as "not changed / not touched".
FW_LANDED=""
_fw_precheck() {
  local name="$1" root="$2" to="$3"
  local conf="$root/.instance.conf" ch now eq_rc=0 ge_rc=0 plc
  FW_SU=""; FW_NOW=""; FW_BLOCK_REASON=""

  if [[ ! -f "$conf" || ! -r "$conf" ]]; then
    _log "  framework $name: BLOCKED -- .instance.conf 판독 불가 ($conf), fail-closed"
    return 1
  fi
  # Channel re-read: a channel=main instance is never a tag-candidate target
  # (planner SKIP-MAIN); seeing one here means the conf changed in the
  # plan-apply window -- fail closed, never pull-apply.
  ch="$(_conf_get "$conf" DOGANY_UPDATE_CHANNEL)"
  if [[ "$ch" == "main" ]]; then
    _log "  framework $name: BLOCKED -- 적용 시점 채널이 main (태그 후보 대상 아님. 계획-적용 창 사이 변동 의심), fail-closed"
    return 1
  fi
  now="$(_conf_get "$conf" DOGANY_FW_VERSION)"
  if [[ -z "$now" ]]; then
    _log "  framework $name: BLOCKED -- 적용 시점 재판독에서 DOGANY_FW_VERSION 부재, fail-closed"
    return 1
  fi
  # DGN-1723: precedence compare (semver_range.py compare), never the ==/>=
  # range lane (prefix parse: 2.6.0-dev.55 read EQUAL to a recorded 2.6.0).
  # Exact installed build = DOGANY_FW_TAG while its X.Y.Z still equals
  # DOGANY_FW_VERSION (update.sh fw_downgrade_guard rule); else FW_VERSION and
  # a same-X.Y.Z pre-release candidate is not judged a downgrade/stale.
  local nowtag now_eff pcmp pcmp_rc=0
  nowtag="$(_conf_get "$conf" DOGANY_FW_TAG)"
  now_eff="$now"
  if [[ "$nowtag" == v[0-9]* && "$(_fw_base "${nowtag#v}")" == "$(_fw_base "$now")" ]]; then
    now_eff="${nowtag#v}"
  fi
  _log "  framework $name: 적용 시점 재판독 -- 설치 $now_eff vs 후보 $to (두 문자열 실비교)"
  pcmp="$("$PACK_PY3" "$SEMVER_LIB" compare "$to" "$now_eff" 2>/dev/null)" || pcmp_rc=$?
  if [[ "$pcmp_rc" -ne 0 || -z "$pcmp" ]]; then
    _log "  framework $name: BLOCKED -- 버전 비교 불가 (후보 '$to' vs 설치 '$now_eff'), fail-closed"
    return 1
  fi
  if [[ "$pcmp" == "-1" && "$now_eff" == "$now" && "$(_fw_base "$to")" == "$now" && "$to" == *-* ]]; then
    _log "  framework $name: 설치 $now 는 정확한 빌드(DOGANY_FW_TAG)를 알 수 없어 같은 X.Y.Z 의 프리릴리스 후보 $to 는 다운그레이드로 판정하지 않음"
    pcmp=1
  fi
  if [[ "$pcmp" == "0" ]]; then
    if [[ " $FW_LANDED " == *" $root=$now_eff "* ]]; then
      _log "  framework $name: already landed by this run -- installed $now_eff == candidate, verified by an earlier unit of this run. Counted as updated, not blocked (no second stop, no second write)"
      FW_NOW="$now_eff"
      return 3
    fi
    # Installed already equals the candidate and this run did not put it
    # there: the member IS on the target. Say that, not "not touched".
    FW_BLOCK_REASON="not changed by this update: it was already on this version when this update reached it (installed by another update run)"
    _log "  framework $name: BLOCKED-STALE-PLAN -- 후보 == 설치($now_eff). 계획 이후 설치본이 변동됨 (조용한 성공으로 세지 않음)"
    return 1
  fi
  if [[ "$pcmp" == "-1" ]]; then
    _log "  framework $name: BLOCKED-DOWNGRADE -- 후보 $to < 설치 $now_eff. 다운그레이드는 절대 적용하지 않습니다 (fail-closed)"
    return 1
  fi
  FW_SU="${SELFUPD_OVERRIDE:-$root/routines/self-update.sh}"
  if [[ ! -f "$FW_SU" || ! -r "$FW_SU" ]]; then
    _log "  framework $name: BLOCKED -- self-update 진입점 부재 ($FW_SU). 프레임워크 적용은 self-update 경로 전량 위임이므로 진입점 없이 적용하지 않습니다 (fail-closed)"
    return 1
  fi
  plc="$(_plist_count "$root")"
  if [[ "$plc" -eq 0 ]]; then
    _log "  framework $name: BLOCKED -- 재시작 대상 plist 0건 ($root) -- 0건은 통과가 아니라 의심 (fail-closed)"
    return 1
  fi
  FW_NOW="$now_eff"   # DGN-1723: exact installed build (tag), not the bare recorded X.Y.Z
  return 0
}

_fw_base() { printf '%s' "${1%%[-+]*}"; }   # X.Y.Z of X.Y.Z[-pre][+build]

# _fw_payload <name> <root> <to> <su> -- the delegated application itself.
# rc 0 landed+verified / 2 incomplete (logged; caller keeps the bridge down).
_fw_payload() {
  local name="$1" root="$2" to="$3" su="$4"
  local conf="$root/.instance.conf" out="$TMP/fw.$UNIT_N.$name.out" rc=0 rec rec_tag
  _log "  framework $name: self-update.sh 위임 (--no-restart, DOGANY_UPDATE_PIN=v$to -- 계획 후보 고정)"
  # DOGANY_UPDATE_DOOR=apply: this ledger IS the estate's one apply door
  # (self-update.sh refuses a direct run on an estate host, DGN-1417).
  DOGANY_UPDATE_DOOR=apply DOGANY_UPDATE_PIN="v$to" bash "$su" --no-restart < /dev/null > "$out" 2>&1 || rc=$?
  sed 's/^/[apply]     /' "$out"
  if [[ "$rc" -ne 0 ]]; then
    _log "  framework $name: 미완주 -- self-update rc=$rc. 브리지 재시작하지 않음, 정지 유지 (반쪽 상태 라이브 복귀 금지)."
    _log "  framework $name: 수동 복구 후 재기동: $MINT_RUN start --root $root"
    return 2
  fi
  # Belt 2 (fw wording): update.sh runs fw_reqframework_guard unconditionally
  # and its "[fw-reqfw-guard] INFO: packs evaluated: N" line prints for EVERY
  # evaluation including N=0. A rc-0 run without that line did not pass
  # through the shipped gate (old update.sh / laundered path) -- refused.
  if ! grep -qF "[fw-reqfw-guard] INFO: packs evaluated:" "$out"; then
    _log "  framework $name: FATAL -- self-update rc=0인데 [fw-reqfw-guard] 판정 라인 부재 (게이트 미통과 경로 의심). 성공으로 세지 않음, 정지 유지."
    return 2
  fi
  rec="$(_conf_get "$conf" DOGANY_FW_VERSION)"
  rec_tag="$(_conf_get "$conf" DOGANY_FW_TAG)"
  if [[ "$to" != *-* ]]; then
    _log "  framework $name: 착지 검증 -- DOGANY_FW_VERSION '$rec' vs 후보 '$to' (두 문자열 실비교)"
  else
    _log "  framework $name: 착지 검증 -- DOGANY_FW_VERSION '$rec' + DOGANY_FW_TAG '$rec_tag' vs 후보 '$to' (프리릴리스: X.Y.Z + v태그 실비교)"
  fi
  if [[ "$to" == *-* ]]; then
    # DGN-1723: pre-release candidate. update.sh stamps DOGANY_FW_VERSION with
    # the bare X.Y.Z (REPO_VERSION) and the exact build in DOGANY_FW_TAG.
    if [[ "$rec" != "$(_fw_base "$to")" || "$rec_tag" != "v$to" ]]; then
      _log "  framework $name: FATAL -- 적용 후 기록(VERSION '$rec', TAG '$rec_tag')이 후보('$to' = v$to)와 불일치. 성공으로 세지 않음, 정지 유지."
      return 2
    fi
  elif [[ "$rec" != "$to" ]]; then
    _log "  framework $name: FATAL -- 적용 후 기록('$rec')이 후보('$to')와 불일치. 성공으로 세지 않음, 정지 유지."
    return 2
  fi
  FW_LANDED="${FW_LANDED:+$FW_LANDED }$root=$to"
  return 0
}

# _apply_fw <name> <root> <from> <to> -- standalone framework atomic unit.
# returns: 0 OK (restart DEFERRED to loop end) / 1 BLOCKED / 2 INCOMPLETE
#          / 3 already landed by an earlier unit of this run (counted OK)
_apply_fw() {
  local name="$1" root="$2" from="$3" to="$4"
  local plc rc=0

  local prc=0
  _fw_precheck "$name" "$root" "$to" || prc=$?
  [[ "$prc" -eq 3 ]] && return 3   # DGN-1417: landed earlier in this run
  [[ "$prc" -eq 0 ]] || return 1
  plc="$(_plist_count "$root")"

  if [[ "$DRY" -eq 1 ]]; then
    _log "  framework $name: (dry-run) 계획 -- 정지 plist ${plc}건 -> self-update.sh --no-restart (DOGANY_UPDATE_PIN=v$to) -> 착지 검증(DOGANY_FW_VERSION) -> 재시작은 루프 끝으로 유예"
    _log "  framework $name: (dry-run) self-update 미호출 -- dry-run에서도 git fetch(공유 레포 ref 쓰기)가 발생하므로 호출 자체를 생략 (쓰기 0건 계약)"
    _log "  framework $name: (dry-run) 적용 예정 -- 쓰기/정지/재시작 실행 안 함"
    return 0
  fi

  _log "  framework $name: 브리지 정지 (plist ${plc}건) -- 원자 단위 진입"
  if ! _launchd_stop "$root"; then
    _log "  framework $name: BLOCKED -- 브리지 정지 검증 실패. 라이브 브리지 밑에서 프레임워크를 교체하지 않습니다 (fail-closed, 쓰기 0건 상태)"
    return 1
  fi

  _fw_payload "$name" "$root" "$to" "$FW_SU" || rc=$?
  [[ "$rc" -eq 0 ]] || return "$rc"

  DEFER_FW_ROOTS="${DEFER_FW_ROOTS:+$DEFER_FW_ROOTS }$name:$root"
  _log "  framework $name: 완주(재시작 유예) -- $FW_NOW -> $to. 재시작은 루프 끝 일괄 flush (동일 인스턴스 후속 단위와 합류, 에이전트당 재시작 1회)"
  return 0
}

# _apply_unit <pid> <root> <name> <cand_tag> <cand_ver> <payload_dir>
# returns: 0 OK / 1 BLOCKED / 2 INCOMPLETE
_apply_unit() {
  local pid="$1" root="$2" name="$3" cand_tag="$4" cand_ver="$5" pdir="$6"
  local conf="$root/.instance.conf" slug inst_now rc plc

  slug="$(_conf_get "$conf" DOGANY_AGENT_NAME)"
  if [[ -z "$slug" ]]; then
    _log "  $name/$pid: BLOCKED -- DOGANY_AGENT_NAME 부재 ($conf), 설치 슬러그 해석 불가 (fail-closed)"
    return 1
  fi

  # Fresh installed version -- the plan snapshot is never trusted here.
  inst_now="$(_installed_ver "$conf" "$pid")"
  if [[ -z "$inst_now" ]]; then
    _log "  $name/$pid: BLOCKED -- 적용 시점 재판독에서 DOGANY_PACKS에 $pid@ 엔트리 없음 (계획-적용 창 사이 변동 의심, fail-closed)"
    return 1
  fi
  # DGN-1456/1460: 적용 전 설치 증거 재판독 -- plan 이 봤더라도 다시 본다
  # (계획-적용 창 사이 변동은 위 버전 재판독과 같은 이유로 재검증 대상).
  # MISMATCH/INCOMPLETE/CHECK_ERROR = 이 단위 BLOCKED: 증거가 부정하는 설치
  # 상태 위에 업그레이드를 쌓지 않는다. 자동수리는 하지 않는다(보고서 §3).
  local ev ev_v ev_d
  ev="$(evidence_consume "$root" "$pid" "$inst_now")"
  ev_v="${ev%%|*}"; ev_d="${ev#*|}"
  case "$ev_v" in
    VERIFIED) _log "  $name/$pid: 적용 전 설치 증거 VERIFIED ($ev_d)" ;;
    UNKNOWN)  _log "  $name/$pid: WARN -- 적용 전 설치 증거 UNKNOWN (receipt 이전 legacy 흔적만). 이번 적용이 완주하면 receipt 가 발급된다 ($ev_d)" ;;
    *)
      _log "  $name/$pid: BLOCKED -- 적용 전 설치 증거 $ev_v ($ev_d). 장부를 설치 사실로 읽지 않음, 별도 복구 후 재시도 (DGN-1456/1460 fail-closed)"
      return 1 ;;
  esac

  _log "  $name/$pid: 적용 시점 재판독 -- 설치 $inst_now vs 후보 $cand_ver (두 문자열 실비교)"
  # Precedence compare (semver_range.py compare, SemVer §11 -- dsp t2pin):
  # the old ==/>= range lane prefix-parsed versions, so a pre-release ledger
  # entry (0.11.0-dev.1) read EQUAL to the stable 0.11.0 candidate and the
  # dev -> stable upgrade was blocked as STALE-PLAN. compare orders a
  # pre-release BELOW its release; unparseable = rc 2 = fail-closed.
  local pcmp_rc=0 pcmp
  pcmp="$("$PACK_PY3" "$SEMVER_LIB" compare "$cand_ver" "$inst_now" 2>/dev/null)" || pcmp_rc=$?
  if [[ "$pcmp_rc" -ne 0 || -z "$pcmp" ]]; then
    _log "  $name/$pid: BLOCKED -- 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$inst_now'), fail-closed"
    return 1
  fi
  if [[ "$pcmp" == "0" ]]; then
    _log "  $name/$pid: BLOCKED-STALE-PLAN -- 후보 == 설치($inst_now). 계획 이후 설치본이 변동됨 (조용한 성공으로 세지 않음)"
    return 1
  fi
  if [[ "$pcmp" == "-1" ]]; then
    _log "  $name/$pid: BLOCKED-DOWNGRADE -- 후보 $cand_ver < 설치 $inst_now. 다운그레이드는 절대 적용하지 않습니다 (표류 가드와 동일 방향, fail-closed)"
    return 1
  fi

  # Restart-target presence: 0 plists is suspicion, not a pass (§6).
  plc="$(_plist_count "$root")"
  if [[ "$plc" -eq 0 ]]; then
    _log "  $name/$pid: BLOCKED -- 재시작 대상 plist 0건 ($root) -- 0건은 통과가 아니라 의심 (fail-closed)"
    return 1
  fi

  # Belt 1 re-assert immediately before the installer call (spec §4).
  if [[ ! -f "$COMPAT_LINT" || ! -r "$COMPAT_LINT" ]]; then
    _log "  $name/$pid: 호환성 검사기(compat-lint.sh)가 없어 이 항목을 중단합니다 -- fail-closed. 프레임워크 설치가 불완전한 상태입니다."
    return 1
  fi

  if [[ "$DRY" -eq 1 ]]; then
    _log "  $name/$pid: (dry-run) 계획 -- 정지 plist ${plc}건 -> pack_install --upgrade (--pack-dir $pdir) -> mint_run.sh start"
    local drc=0 dout="$TMP/dry.$UNIT_N.out"
    DOGANY_STRICT_GATES=1 bash "$INSTALLER" "$slug" "$root" \
      --pack "$pid" --pack-dir "$pdir" --source-tag "$cand_tag" \
      ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
      --upgrade --no-start --no-state --dry-run \
      < /dev/null > "$dout" 2>&1 || drc=$?
    sed 's/^/[apply]     /' "$dout"
    if [[ "$drc" -ne 0 ]]; then
      _log "  $name/$pid: BLOCKED -- pack_install --dry-run 플랜 FAIL (rc=$drc)"
      return 1
    fi
    _log "  $name/$pid: (dry-run) 적용 예정 -- 쓰기/정지/재시작 실행 안 함"
    return 0
  fi

  # ---- real apply: stop -> install -> verify -> restart --------------------
  _log "  $name/$pid: 브리지 정지 (plist ${plc}건) -- 원자 단위 진입"
  if ! _launchd_stop "$root"; then
    _log "  $name/$pid: BLOCKED -- 브리지 정지 검증 실패. 라이브 브리지 밑에서 파일을 교체하지 않습니다 (fail-closed, 쓰기 0건 상태)"
    return 1
  fi

  local iout="$TMP/install.$UNIT_N.out" istate="$TMP/install.$UNIT_N.state" irc=0
  rm -f "$istate"
  _log "  $name/$pid: pack_install --upgrade 위임 (payload=$cand_tag)"
  DOGANY_INSTALL_STATE_FILE="$istate" DOGANY_STRICT_GATES=1 bash "$INSTALLER" "$slug" "$root" \
    --pack "$pid" --pack-dir "$pdir" --source-tag "$cand_tag" \
    ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
    --upgrade --no-start --no-state \
    < /dev/null > "$iout" 2>&1 || irc=$?
  sed 's/^/[apply]     /' "$iout"

  # DGN-1895: a refusal BEFORE pack_install's write boundary (its own
  # machine fact, writes=none -- e.g. the DGN-1109 content-drift guard) left
  # the instance exactly as the stop found it: that is a rollback, not a
  # half state. Restart what this unit stopped, now. Anything short of the
  # positive fact (absent/empty/begun, or a rc that is not _fail's 1) keeps
  # the stay-down policy below.
  if [[ "$irc" -eq 1 && "$(cat "$istate" 2>/dev/null)" == "writes=none" ]]; then
    _log "  $name/$pid: BLOCKED -- pack_install이 쓰기 전에 거부 (rc=1, writes=none). 바뀐 것 없음, 이 단위가 정지한 유닛을 되돌림 (DGN-1895)"
    if _restart "$root"; then
      N_RESTART=$((N_RESTART + 1))
      UNIT_REFUSAL=restarted
      _log "  $name/$pid: 미적용 -- 설치 $inst_now 유지, 브리지 재시작 완료"
    else
      N_RESTART_FAIL=$((N_RESTART_FAIL + 1))
      OVERALL_RC=2
      UNIT_REFUSAL=restart_failed
      _log "  $name/$pid: 미적용 -- 설치 $inst_now 유지, 재시작 실패. 수동 재기동 필요: $MINT_RUN start --root $root"
    fi
    return 1
  fi

  if [[ "$irc" -ne 0 ]]; then
    _log "  $name/$pid: 미완주 -- pack_install rc=$irc. 브리지 재시작하지 않음, 정지 유지 (반쪽 상태 라이브 복귀 금지)."
    _log "  $name/$pid: 수동 복구 후 재기동: $MINT_RUN start --root $root"
    return 2
  fi

  # Belt 2 (DGN-1682: every install path's own PASS wording, see
  # _install_gate_passed): a rc-0 run without it means the fail-open
  # WARN+skip path fired mid-run -- laundering refused.
  if ! _install_gate_passed "$iout"; then
    _log "  $name/$pid: FATAL -- pack_install rc=0인데 compat-lint install-side gate PASS 라인 부재 (fail-open 경로 의심). 성공으로 세지 않음, 정지 유지."
    return 2
  fi

  # Post-apply record verification: both strings printed, then compared.
  local rec
  rec="$(_installed_ver "$conf" "$pid")"
  _log "  $name/$pid: 착지 검증 -- DOGANY_PACKS 기록 '$rec' vs 후보 '$cand_ver'"
  if [[ "$rec" != "$cand_ver" ]]; then
    _log "  $name/$pid: FATAL -- 적용 후 기록($rec)이 후보($cand_ver)와 불일치. 성공으로 세지 않음, 정지 유지."
    return 2
  fi

  # DGN-1456/1460: 착지 검증 2 -- 장부 문자열 대조(위)는 장부-후보 대조일 뿐
  # 실물 대조가 아니다(보고서 §1.4). rc=0 이어도 delivery-complete receipt 가
  # 후보 버전으로 VERIFIED 되지 않으면 완주로 세지 않는다.
  ev="$(evidence_consume "$root" "$pid" "$cand_ver")"
  ev_v="${ev%%|*}"; ev_d="${ev#*|}"
  if [[ "$ev_v" != "VERIFIED" ]]; then
    _log "  $name/$pid: FATAL -- 적용 후 설치 증거 $ev_v ($ev_d). receipt 없는 성공은 성공이 아님, 정지 유지 (DGN-1460)."
    return 2
  fi
  _log "  $name/$pid: 적용 후 설치 증거 VERIFIED ($ev_d)"

  # DGN-1710: a later plan line stops this root again (framework-last order,
  # DGN-1417) -- restarting now would only be undone by that stop, and the
  # agent would come back twice. Defer to the loop-end flush instead.
  if _touched_later "$root"; then
    DEFER_UNIT_ROOTS="${DEFER_UNIT_ROOTS:+$DEFER_UNIT_ROOTS }$name/$pid:$root"
    _log "  $name/$pid: 완주(재시작 유예) -- $inst_now -> $cand_ver. 이후 계획 단위가 같은 인스턴스를 다시 정지하므로 재시작은 루프 끝 일괄 flush (에이전트당 재시작 1회)"
    return 0
  fi

  if _restart "$root"; then
    N_RESTART=$((N_RESTART + 1))
    _log "  $name/$pid: 완주 -- $inst_now -> $cand_ver, 브리지 재시작 완료"
  else
    N_RESTART_FAIL=$((N_RESTART_FAIL + 1))
    OVERALL_RC=2
    _log "  $name/$pid: 적용은 완주했으나 재시작 실패 -- 수동 재기동 필요: $MINT_RUN start --root $root"
  fi
  return 0
}

# Print one executable command; extraction is recreated from the retained tag,
# never from this apply run's temporary directory. Each step fails closed.
_crew_recovery_command() {
  local kit="$1" pid="$2" members="$3" repo="$4" tag="$5" m root slug cmd part
  cmd='set -euo pipefail; '
  printf -v part 'export DOGANY_SHARED_HOME=%q DOGANY_CREW_DIR=%q DOGANY_STRICT_GATES=1; ' "$SHARED_HOME" "$CREW_DIR"
  cmd+="$part"
  cmd+='recovery_dir=$(mktemp -d); '
  printf -v part 'git -C %q archive --format=tar %q | tar -xf - -C "$recovery_dir"; ' "$repo" "refs/tags/$tag"
  cmd+="$part"
  for m in $members; do
    root="${m#*:}"; slug="$(_conf_get "$root/.instance.conf" DOGANY_AGENT_NAME)"
    printf -v part 'bash %q %q %q --pack %q --pack-dir "$recovery_dir" --source-tag %q ' "$INSTALLER" "$slug" "$root" "$pid" "$tag"
    cmd+="$part"
    if [[ ${#CATALOG_ARGS[@]} -gt 0 ]]; then
      printf -v part '%q ' "${CATALOG_ARGS[@]}"; cmd+="$part"
    fi
    cmd+='--upgrade --no-start --no-state; '
  done
  printf -v part 'DOGANY_SHARED_HOME=%q DOGANY_CREW_DIR=%q bash %q --clear-incomplete %q; ' "$SHARED_HOME" "$CREW_DIR" "$SCRIPT_DIR/update_apply.sh" "$kit"
  cmd+="$part"
  cmd+='rm -rf "$recovery_dir"'
  printf '[apply] RECOVERY: bash -c %q\n' "$cmd"
}

# _crew_incomplete <kit> <pid> <member> <reason> <backup> <members>
# INCOMPLETE crew transaction surface: every crew bridge stays down, the T0
# backup path and manual recovery steps are printed (spec §3 rollback story
# item 2 -- no automatic restore, first cut is a manual procedure).
_crew_incomplete() {
  local kit="$1" pid="$2" mname="$3" reason="$4" bak="$5" members="$6" m
  # DGN-1509 B2: DIRTY -> INCOMPLETE. The lease was already written before
  # T0-7 (see _apply_crew) -- this just records WHY, for the human reading
  # the lease later. The lease stays on disk either way (never auto-cleared
  # on failure); this call only fails loud if _apply_crew somehow reached
  # here without having written the lease first (a bug in THIS file, not a
  # runtime condition -- still handled fail-closed, never silently skipped).
  _crew_lease_mark_incomplete "$kit" "$mname: $reason" \
    || _log "  크루 $kit/$pid: WARN -- lease를 INCOMPLETE로 전이하지 못함 (위 crew-lease 로그 참조); 브리지는 아래 정책대로 여전히 내려간 채 유지됩니다"
  _log "  크루 $kit/$pid: 미완주 -- $mname: $reason"
  _log "  크루 $kit/$pid: 크루 전 멤버 브리지 재시작하지 않음, 정지 유지 (반쪽 상태 라이브 복귀 금지 -- 원자 단위 = 크루 전체)"
  _log "  크루 $kit/$pid: 복구 절차 (1) 공유 DB 복원이 필요하면 T0 백업 사용: $bak"
  _log "Recovery: resolve the cause, then run this forward-only command (all members, then verified lease release)."
  _crew_recovery_command "$kit" "$pid" "$conf_members" "$repo" "$cand_tag"
  if [[ -n "${fw_units:-}" ]]; then
    _log "Atomic framework group: finish the planned framework updates before re-arming the group."
  else
    for m in $members; do
      printf '[apply] After recovery succeeds: bash %q start --root %q\n' "$MINT_RUN" "${m#*:}"
    done
  fi
}

# _apply_crew <pid> <kind> <provides> <cand_tag> <cand_ver> <plan_members>
#             [fw_units] [sync_decl]
# sync_decl (dec-263, header CREW SYNC): "name=ver ..." -- the planned crew
# sync's per-member versions; empty = the symmetric-or-ABORT crew update.
# The WHOLE crew is ONE atomic unit (spec §3 T0~T-end). See the header
# CREW TRANSACTION block for the full contract.
# fw_units (slice 4, ATOMIC GROUP generalization): space-separated
# "name;root;from;to" framework items applied INSIDE this transaction --
# same stop/backup/restart envelope, packs FIRST then framework (see the
# header ATOMIC GROUP block for why fw-first is structurally blocked by the
# shipped fw_reqframework_guard). Callers MUST have run _fw_precheck on
# every fw unit already (read-only, pre-entry). Empty = plain crew unit.
# returns: 0 OK / 1 BLOCKED (pre-entry, zero instance writes) / 2 INCOMPLETE
# (transaction entered and did not complete -- EVERY group bridge stays down).
# Cross-source discovery mismatches / member-set drift / member asymmetry
# ABORT the whole run (estate state itself is suspect, spec §3 fail-closed).
_apply_crew() {
  local pid="$1" pkind="$2" provides="$3" cand_tag="$4" cand_ver="$5" plan_members="$6"
  local fw_units="${7:-}"
  local sync_decl="${8:-}"
  local crew_kit="${provides:-$pid}"
  local crew_home="$SHARED_HOME/crews/$crew_kit"
  local crew_conf="$CREW_DIR/$crew_kit/crew.conf"
  local crew_db="$crew_home/$crew_kit.db"
  local m mname mroot cl link m_packs arc lrc

  _log "  크루 $crew_kit/$pid: T0 진입 전 전수 검사 -- 원자 단위 = 크루 전체 (개별 멤버 아님)"

  if [[ -z "${plan_members// /}" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 계획 항목에 멤버 0건 (0건은 통과가 아니라 의심), fail-closed"
    return 1
  fi
  if [[ ! -f "$crew_conf" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- crew.conf 부재 ($crew_conf). 크루 스코프 항목인데 크루 레지스트리가 없음 (시드: update_seed.sh), fail-closed"
    return 1
  fi

  # ---- T0-1: apply-time crew re-discovery + 3-source cross-validation ----
  # The plan snapshot is NEVER trusted here (same doctrine as the fresh
  # version re-read on the instance path). Sources: (1) crew.conf line,
  # (2) member DOGANY_PACKS has <pack>@, (3) readlink symlink ground truth.
  local conf_members="" member_n=0
  while IFS= read -r cl; do
    case "$cl" in
      member=*) : ;;
      ''|'#'*) continue ;;
      *) _abort "크루 $crew_kit: crew.conf 라인 파싱 실패: '$cl' -- fail-closed" ;;
    esac
    mroot="${cl#member=}"
    mname="$(basename "$mroot")"
    for m in $plan_members; do
      if [[ "${m#*:}" == "$mroot" ]]; then mname="${m%%:*}"; fi
    done
    member_n=$((member_n + 1))
    conf_members="${conf_members:+$conf_members }$mname:$mroot"
    _log "  크루 $crew_kit: 교차검증 $mname source1(crew.conf) PASS"
    m_packs="$(_conf_get "$mroot/.instance.conf" DOGANY_PACKS)"
    case ",$m_packs," in
      *",$pid@"*) _log "  크루 $crew_kit: 교차검증 $mname source2(DOGANY_PACKS) PASS" ;;
      *) _log "  크루 $crew_kit: 교차검증 $mname source2(DOGANY_PACKS) FAIL"
         _abort "크루 $crew_kit: $mname DOGANY_PACKS에 $pid@ 엔트리 없음 -- 3소스 불일치, ABORT (fail-closed, 쓰기 0건)" ;;
    esac
    link="$(readlink "$mroot/database/$crew_kit.db" 2>/dev/null || echo "")"
    if [[ "$link" == "$crew_db" ]]; then
      _log "  크루 $crew_kit: 교차검증 $mname source3(symlink) PASS ($link)"
    else
      _log "  크루 $crew_kit: 교차검증 $mname source3(symlink) FAIL (readlink='$link' != '$crew_db')"
      _abort "크루 $crew_kit: $mname symlink ground truth 불일치 -- 3소스 불일치, ABORT (fail-closed, 쓰기 0건)"
    fi
  done < "$crew_conf"
  _log "  크루 $crew_kit 멤버 ${member_n}건${conf_members:+ ($conf_members)}"
  if [[ "$member_n" -eq 0 ]]; then
    _abort "크루 $crew_kit: 멤버 0건 = ABORT (0건은 통과가 아니라 의심, DGN-1030 §주의)"
  fi

  # plan-vs-conf member-set drift, both directions (plan-apply window).
  local pm cm found
  for pm in $plan_members; do
    found=0
    for cm in $conf_members; do
      if [[ "${cm#*:}" == "${pm#*:}" ]]; then found=1; fi
    done
    if [[ "$found" -ne 1 ]]; then
      _abort "크루 $crew_kit: 계획 멤버 ${pm%%:*}(${pm#*:})가 적용 시점 crew.conf에 없음 -- 계획-적용 창 사이 크루 구성 변동, ABORT (쓰기 0건)"
    fi
  done
  for cm in $conf_members; do
    found=0
    for pm in $plan_members; do
      if [[ "${pm#*:}" == "${cm#*:}" ]]; then found=1; fi
    done
    if [[ "$found" -ne 1 ]]; then
      _abort "크루 $crew_kit: 적용 시점 crew.conf 멤버 ${cm%%:*}(${cm#*:})가 계획에 없음 -- 계획-적용 창 사이 크루 구성 변동, ABORT (쓰기 0건)"
    fi
  done

  # ---- T0-1d: DB-sharer completeness (DGN-1517 F-2) ----
  # The crew-lockstep guard trusts the HOME estate scan + physical DB
  # identity, NOT crew.conf (pack_install.sh guard header: "crew.conf is
  # deliberately NOT read here"); this transaction trusts crew.conf. A
  # sharer of this crew's DB that crew.conf does not list (mid-join, missed
  # seed) passes every conf-derived T0 check INCLUDING T0-5's dry-run --
  # evidence class 3b runs before the lease exists, so the dry-run cannot
  # check lease membership and accepts the sharer as PREVIEW-JOINT -- and is
  # then refused only by member #1's REAL preflight, AFTER the bridges are
  # down, the T0 backup exists and the lease is DIRTY: a measured
  # mid-transaction INCOMPLETE with no implemented recovery, where the
  # pre-DGN-1517 tree refused the same input at T0-5 with zero writes.
  # Restore that failure position: ask the guard's own question ("who
  # shares this physical DB?") with the guard's own sources (same
  # _discover_instances scan, same physical-file identity) BEFORE entry,
  # and ABORT with zero writes when any sharer is not a declared member.
  # Runs in dry-run too, so the dry-run verdict stays an exact prediction
  # of the real run's (3b's stated contract). Doctrine slot: this is a
  # cross-source discovery mismatch -- "estate state itself is suspect"
  # (_apply_crew contract above) -- hence _abort, not a per-unit BLOCKED.
  local disc_scan disc_line disc_name disc_root disc_db crew_db_real other_real cm_real
  if ! disc_scan="$(_discover_instances)"; then
    _abort "크루 $crew_kit: HOME estate 스캔 실패 -- 공유 DB 의 공유자 전수를 확인할 수 없음, fail-closed ABORT (쓰기 0건, DGN-1517 F-2)"
  fi
  crew_db_real="$("$PACK_PY3" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$crew_db")"
  while IFS= read -r disc_line; do
    [[ -n "$disc_line" ]] || continue
    disc_name="${disc_line%%:*}"
    disc_root="${disc_line#*:}"
    disc_db="$disc_root/database/$crew_kit.db"
    [[ -e "$disc_db" || -L "$disc_db" ]] || continue
    other_real="$("$PACK_PY3" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$disc_db")"
    [[ "$other_real" == "$crew_db_real" ]] || continue
    found=0
    for cm in $conf_members; do
      cm_real="$(cd -P "${cm#*:}" 2>/dev/null && pwd)" || cm_real="${cm#*:}"
      if [[ "$cm_real" == "$disc_root" ]]; then found=1; fi
    done
    if [[ "$found" -ne 1 ]]; then
      _abort "크루 $crew_kit: 발견 스캔이 공유 DB($crew_db)의 공유자 '$disc_name'($disc_root)를 보는데 crew.conf 멤버가 아님 -- 미등록 공유자는 T0-5 dry-run 을 통과(3b 는 lease 이전이라 멤버십 검사 불가)한 뒤 실제 실행의 멤버 preflight 에서야 거부되어 트랜잭션이 mid-flight 미완주(전 브리지 다운 + INCOMPLETE lease)로 강등되므로, 진입 전에 차단합니다. 조인을 완료(crew.conf 등록, update_seed.sh)하거나 해당 인스턴스의 DB 공유를 해소한 뒤 재시도하세요. ABORT (쓰기 0건, DGN-1517 F-2)"
    fi
  done <<< "$disc_scan"
  _log "  크루 $crew_kit: T0-1d 공유자 전수 검사 PASS -- 이 DB 의 물리 공유자 전원이 crew.conf 멤버 (가드와 동일 소스: _discover_instances + 물리 DB 동일성, DGN-1517 F-2)"

  # ---- T0-1c: DGN-1506 item 2 -- consent is a record, not a recollection.
  # A crew apply pauses EVERY member bridge; the user's yes to that pause is
  # taken ONCE, up front, in the conversation (model lane -- mint-agent
  # SKILL.md step 2), and recorded by the separate approval verb
  # scripts/pack/crew_consent.sh. THIS check is the rule lane: absent /
  # expired / member-drifted token = refused pre-entry (zero writes, zero
  # stops), and the refusal names the missing verb so the skill's own
  # resumed-flow copy ("consent not on record -> ask ONCE with the step-2
  # copy, then continue") can recover with one question + one record call.
  # There is NO env bypass, deliberately (DGN-1509 spec v3 §3.4 "환경변수
  # 단독 무효") -- the model could set an env var, which would be the
  # recollection lane again. This early verify is a fail-fast courtesy; the
  # binding check is _crew_consent_consume immediately before the T0-7 stop
  # (the consented pause), which re-verifies and single-uses the token.
  local consent_roots consent_why
  consent_roots="$(printf '%s\n' $conf_members | sed 's/^[^:]*://')"
  if [[ "$DRY" -eq 1 ]]; then
    # Dry-run pauses nothing, so consent is not consumed OR required here --
    # but the real run's verdict is previewed loudly instead of silently.
    if consent_why="$(_crew_consent_verify "$crew_kit" "$consent_roots")"; then
      _log "  크루 $crew_kit/$pid: (dry-run) 동의 토큰 VALID -- 실제 실행은 이 토큰을 검증 후 1회 소비합니다 (DGN-1506 2번)"
    else
      _log "  크루 $crew_kit/$pid: (dry-run) 동의 토큰 없음/무효 ($consent_why) -- 실제 실행은 crew.consent 게이트에서 거부됩니다 (기록: scripts/pack/crew_consent.sh record --kit $crew_kit)"
    fi
  elif ! consent_why="$(_crew_consent_verify "$crew_kit" "$consent_roots")"; then
    _gate_stop crew.consent "  크루 $crew_kit/$pid: BLOCKED -- 크루 일시정지 동의가 기록에 없음/무효 ($consent_why). 동의는 대화에서 1회 받되 기록 대조로만 인정됩니다 -- 모델의 기억은 동의가 아닙니다 (DGN-1506 2번). 유저의 예를 받은 뒤 scripts/pack/crew_consent.sh record --kit $crew_kit 를 실행하고 재시도하세요 (진입 전 차단, 쓰기 0건, 브리지 무접촉)"
    return 1
  else
    _log "  크루 $crew_kit/$pid: 동의 토큰 검증 PASS (기록 대조, DGN-1506 2번) -- T0-7 정지 직전에 1회 소비합니다"
  fi

  # ---- ATOMIC GROUP extension (slice 4): fw units share this transaction's
  # stop/backup/restart envelope. fw roots NOT among the crew members get
  # their own stop/restart entries; overlapping roots are handled once.
  local fw_n=0 fwonly="" fu funame furoot fu_rest
  local group_members="$conf_members"
  if [[ -n "$fw_units" ]]; then
    for fu in $fw_units; do
      fw_n=$((fw_n + 1))
      funame="${fu%%;*}"; fu_rest="${fu#*;}"; furoot="${fu_rest%%;*}"
      found=0
      for cm in $conf_members; do
        if [[ "${cm#*:}" == "$furoot" ]]; then found=1; fi
      done
      if [[ "$found" -ne 1 ]]; then
        fwonly="${fwonly:+$fwonly }$funame:$furoot"
        group_members="${group_members:+$group_members }$funame:$furoot"
      fi
    done
    _log "  크루 $crew_kit/$pid: ATOMIC GROUP -- framework ${fw_n}건이 같은 트랜잭션에 동승 (그룹 전체 = 최소 적용 단위, 내부 순서 = 팩 -> 프레임워크)"
  fi

  # ---- T0-2: fresh per-member installed versions (symmetric or ABORT) ----
  # dec-263 crew sync: asymmetric as DECLARED by the plan line, else ABORT.
  local inst_now="" v sd decl sync_vers="" s_repo s_cmp s_rc s_below=0
  if [[ -n "$sync_decl" ]]; then
    s_repo="$(_reg_repo "$pid")"
  fi
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    v="$(_installed_ver "$mroot/.instance.conf" "$pid")"
    if [[ -z "$v" ]]; then
      _abort "크루 $crew_kit: $mname 적용 시점 재판독에서 DOGANY_PACKS $pid@ 엔트리 없음 -- 3소스 불일치, ABORT"
    fi
    if [[ -n "$sync_decl" ]]; then
      decl=""
      for sd in $sync_decl; do
        if [[ "${sd%%=*}" == "$mname" ]]; then decl="${sd#*=}"; fi
      done
      if [[ "$decl" != "$v" ]]; then
        _log "  크루 $crew_kit/$pid: BLOCKED-STALE-PLAN -- 버전 맞추기 계획의 $mname 설치본('${decl:-없음}') != 적용 시점 '$v' -- 계획 이후 크루가 변동됨, 다시 계획하세요 (dec-263, 쓰기 0건)"
        return 1
      fi
      if [[ "$v" == *-* || "$v" == *+* ]] \
          || [[ -z "$s_repo" ]] \
          || ! git -C "$s_repo" rev-parse -q --verify "refs/tags/pack/$pid/v$v" >/dev/null 2>&1; then
        _log "  크루 $crew_kit/$pid: BLOCKED -- $mname=$v 는 정식판 태그 설치가 아님 (시험/태그 없는 착지) -- 이 크루의 버전 맞추기는 운영자 전용 (dec-263, 쓰기 0건)"
        return 1
      fi
      s_rc=0
      s_cmp="$("$PACK_PY3" "$SEMVER_LIB" compare "$cand_ver" "$v" 2>/dev/null)" || s_rc=$?
      if [[ "$s_rc" -ne 0 || -z "$s_cmp" || "$s_cmp" == "-1" ]]; then
        _log "  크루 $crew_kit/$pid: BLOCKED-DOWNGRADE -- 버전 맞추기 후보 $cand_ver 가 $mname=$v 보다 낮거나 비교 불가 -- 다운그레이드는 절대 적용하지 않습니다 (dec-263, fail-closed)"
        return 1
      fi
      if [[ "$s_cmp" == "1" ]]; then s_below=1; fi
      sync_vers="${sync_vers:+$sync_vers, }$mname=$v"
      continue
    fi
    if [[ -z "$inst_now" ]]; then
      inst_now="$v"
    elif [[ "$inst_now" != "$v" ]]; then
      _abort "크루 $crew_kit: 멤버 간 설치 버전 불일치 ($inst_now vs $mname=$v) -- 비대칭 크루는 반쪽 갱신 의심, ABORT (fail-closed, 쓰기 0건)"
    fi
  done
  if [[ -n "$sync_decl" ]]; then
    if [[ "$s_below" -ne 1 ]]; then
      _log "  크루 $crew_kit/$pid: BLOCKED-STALE-PLAN -- 버전 맞추기인데 후보 $cand_ver 보다 낮은 멤버가 없음 ($sync_vers) -- 이미 맞춰짐 (조용한 성공으로 세지 않음, dec-263)"
      return 1
    fi
    inst_now="$sync_vers"
    _log "  크루 $crew_kit/$pid: 버전 맞추기 (dec-263) -- 멤버 설치본 ($sync_vers) 이 계획 선언과 일치, 전원 정식판 태그; 전원을 $cand_tag 로 한 트랜잭션에서"
  fi
  # kit-core symmetry probe: every member's installed kit core must carry an
  # IDENTICAL version-pin line (raw string equality -- no version semantics
  # here, the comparator stays pack_install's). An asymmetric crew would
  # otherwise fail MID-transaction (INCOMPLETE, every bridge down); catching
  # it at T0 keeps the failure pre-entry with zero downtime.
  local pin_line ref_pin="" ref_set=0
  for m in $conf_members; do
    # dec-263: a split crew's pins differ by definition -- the lockstep
    # guard (Phase P staging + lease, evidence class 3a) carries the joint
    # move exactly as for a symmetric crew update.
    [[ -z "$sync_decl" ]] || break
    mname="${m%%:*}"; mroot="${m#*:}"
    if [[ -f "$mroot/database/$crew_kit.py" ]]; then
      pin_line="$(grep -E '^EXPECTED_USER_VERSION' "$mroot/database/$crew_kit.py" 2>/dev/null | head -n1)"
      pin_line="${pin_line:-<no-pin-line>}"
    else
      pin_line="<no-kit-core>"
    fi
    if [[ "$ref_set" -eq 0 ]]; then
      ref_pin="$pin_line"; ref_set=1
    elif [[ "$pin_line" != "$ref_pin" ]]; then
      _abort "크루 $crew_kit: 멤버 간 kit core 핀 라인 불일치 ('$ref_pin' vs $mname='$pin_line') -- 비대칭 크루는 반쪽 갱신 의심, ABORT (fail-closed, 쓰기 0건)"
    fi
  done
  if [[ -z "$sync_decl" ]]; then
    _log "  크루 $crew_kit/$pid: kit core 핀 라인 대칭 확인 (멤버 ${member_n}건 동일: '${ref_pin}')"
  else
    _log "  크루 $crew_kit/$pid: kit core 핀 라인 대칭 검사 생략 -- 버전 맞추기 (반쪽 크루의 핀은 원래 다름; 공동 전진은 Phase P + lease 가 보증, dec-263)"
  fi

  # DGN-1456/1460: 적용 전 설치 증거 재판독 -- 멤버 전원. 한 멤버라도
  # MISMATCH/INCOMPLETE/CHECK_ERROR 면 크루 전체 BLOCKED (쓰기 0건 상태에서
  # 중단 -- 반쪽 크루 트랜잭션 진입 금지, DGN-1436 방향과 동일).
  local ev ev_v ev_d
  local ev_at
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    ev_at="$inst_now"
    if [[ -n "$sync_decl" ]]; then
      ev_at="$(_installed_ver "$mroot/.instance.conf" "$pid")"
    fi
    ev="$(evidence_consume "$mroot" "$pid" "$ev_at")"
    ev_v="${ev%%|*}"; ev_d="${ev#*|}"
    case "$ev_v" in
      VERIFIED) _log "  크루 $crew_kit/$pid: $mname 적용 전 설치 증거 VERIFIED ($ev_d)" ;;
      UNKNOWN)  _log "  크루 $crew_kit/$pid: WARN -- $mname 적용 전 설치 증거 UNKNOWN (receipt 이전 legacy 흔적만). 이번 적용 완주 시 receipt 발급 ($ev_d)" ;;
      *)
        _log "  크루 $crew_kit/$pid: BLOCKED -- $mname 적용 전 설치 증거 $ev_v ($ev_d). 장부를 설치 사실로 읽지 않음, 별도 복구 후 재시도 (DGN-1456/1460 fail-closed, 쓰기 0건)"
        return 1 ;;
    esac
  done

  local pcmp_rc=0 pcmp
  if [[ -n "$sync_decl" ]]; then
    # dec-263: compared per member at T0-2 above (>= all, > at least one).
    pcmp=1
  else
    _log "  크루 $crew_kit/$pid: 적용 시점 재판독 -- 설치 $inst_now vs 후보 $cand_ver (두 문자열 실비교, 멤버 ${member_n}건 전원 동일)"
    # Precedence compare (SemVer §11, dsp t2pin) -- same rationale as the
    # instance-scope block in _apply_unit: prefix-parse read a pre-release
    # ledger entry as EQUAL to its stable candidate.
    pcmp="$("$PACK_PY3" "$SEMVER_LIB" compare "$cand_ver" "$inst_now" 2>/dev/null)" || pcmp_rc=$?
  fi
  if [[ "$pcmp_rc" -ne 0 || -z "$pcmp" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 버전 비교 불가 (후보 '$cand_ver' vs 설치 '$inst_now'), fail-closed"
    return 1
  fi
  if [[ "$pcmp" == "0" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED-STALE-PLAN -- 후보 == 설치($inst_now). 계획 이후 설치본이 변동됨 (조용한 성공으로 세지 않음)"
    return 1
  fi
  if [[ "$pcmp" == "-1" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED-DOWNGRADE -- 후보 $cand_ver < 설치 $inst_now. 다운그레이드는 절대 적용하지 않습니다 (fail-closed)"
    return 1
  fi

  # ---- T0-3: shared DB + tooling + restart-target presence ----
  if [[ ! -f "$crew_db" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 공유 DB 실체 부재 ($crew_db): symlink는 있으나 대상 파일이 없음, fail-closed"
    return 1
  fi
  if ! command -v sqlite3 >/dev/null 2>&1; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- sqlite3 부재: T0 공유 DB 백업 불가, fail-closed"
    return 1
  fi
  local plc total_plc=0
  for m in $conf_members; do
    plc="$(_plist_count "${m#*:}")"
    if [[ "$plc" -eq 0 ]]; then
      _log "  크루 $crew_kit/$pid: BLOCKED -- ${m%%:*} 재시작 대상 plist 0건 (${m#*:}) -- 크루 항목에서 0건은 통과가 아니라 의심 (fail-closed)"
      return 1
    fi
    total_plc=$((total_plc + plc))
  done
  _log "  크루 $crew_kit/$pid: 재시작 대상 plist 총 ${total_plc}건 (멤버 ${member_n}건, 0건 아님)"

  # Belt 1 re-assert immediately before installer involvement (spec §4).
  if [[ ! -f "$COMPAT_LINT" || ! -r "$COMPAT_LINT" ]]; then
    _log "  크루 $crew_kit/$pid: 호환성 검사기(compat-lint.sh)가 없어 이 항목을 중단합니다 -- fail-closed. 프레임워크 설치가 불완전한 상태입니다."
    return 1
  fi

  # ---- T0-4: ONE payload extraction + manifest asserts ----
  local repo pdir
  repo="$(_reg_repo "$pid")"
  if [[ -z "$repo" || ! -d "$repo" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 레지스트리 repo 미해석 ('$repo'), fail-closed"
    return 1
  fi
  pdir="$TMP/payload.crew.$UNIT_N"
  mkdir -p "$pdir"
  if ! git -C "$repo" rev-parse -q --verify "refs/tags/$cand_tag" >/dev/null 2>&1; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 후보 태그 $cand_tag 이 repo($repo)에 없음 (계획-적용 창 사이 변동 의심), fail-closed"
    return 1
  fi
  if ! git -C "$repo" archive --format=tar "refs/tags/$cand_tag" | tar -xf - -C "$pdir"; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 태그 $cand_tag payload 추출 실패, fail-closed"
    return 1
  fi
  _log "  크루 $crew_kit/$pid: payload 1회 추출 -> 전 멤버가 같은 추출본에서 설치 (멤버 간 버전 분화 원천 차단)"
  local mfline mf_id mf_kind mf_ver mf_prov
  mfline="$("$PACK_PY3" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
def s(k):
    v = d.get(k)
    return v if isinstance(v, str) else ''
print('\t'.join([s('id'), s('kind'), s('pack_version'), s('provides_kit')]))
" "$pdir/pack-manifest.json" 2>/dev/null || true)"
  mf_id="$(printf '%s' "$mfline" | cut -f1)"
  mf_kind="$(printf '%s' "$mfline" | cut -f2)"
  mf_ver="$(printf '%s' "$mfline" | cut -f3)"
  mf_prov="$(printf '%s' "$mfline" | cut -f4)"
  if [[ -z "$mfline" || "$mf_id" != "$pid" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출 manifest id('$mf_id') != 레지스트리 id('$pid') -- 구독원 오염 의심, fail-closed"
    return 1
  fi
  if [[ "$mf_kind" != "kit" && -z "$mf_prov" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출본이 크루 스코프가 아님 (kind='$mf_kind' provides_kit='') -- 계획과 불일치, fail-closed"
    return 1
  fi
  # Installer KIT_NAME contract (pack_install.sh lockstep, DGN-1143):
  # provides_kit is MANDATORY for a crew-scope (kit) extraction -- the legacy
  # 'lifekit' default is removed (zero published manifests relied on it, and
  # after the dec-145 kit rename it would silently equal a stale crew key).
  # The effective kit name MUST equal the crew key -- KIT_NAME drives every
  # write path incl. crews/<kit>/.
  if [[ -z "$mf_prov" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출 manifest 에 provides_kit 부재 (legacy 'lifekit' 폴백은 DGN-1143 으로 제거됨; 개명 후 폴백은 존재하지 않는 킷을 가리킨다) -- fail-closed"
    return 1
  fi
  local eff_kit="$mf_prov"
  if [[ "$eff_kit" != "$crew_kit" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출본 kit 이름('$eff_kit') != 크루 키('$crew_kit') -- 설치 경로 불일치 위험, fail-closed"
    return 1
  fi
  # 3-point check, pre-release form (dsp t2pin): the tag may ADD a
  # -pre/+build suffix to the manifest version (dogfooding cut), nothing else.
  if [[ -n "$mf_ver" && "$mf_ver" != "$cand_ver" \
        && "$cand_ver" != "$mf_ver"-* && "$cand_ver" != "$mf_ver"+* ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 추출 manifest pack_version($mf_ver) != 태그 버전($cand_ver) (pre-release 접미 초과 발산), 3점 불일치 fail-closed"
    return 1
  fi

  # ---- T0-5: per-member slug + pack_install --dry-run (all must pass) ----
  local slug drc dout un=0
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    slug="$(_conf_get "$mroot/.instance.conf" DOGANY_AGENT_NAME)"
    if [[ -z "$slug" ]]; then
      _log "  크루 $crew_kit/$pid: BLOCKED -- $mname DOGANY_AGENT_NAME 부재 ($mroot/.instance.conf), 설치 슬러그 해석 불가 (fail-closed)"
      return 1
    fi
    un=$((un + 1))
    drc=0; dout="$TMP/crew.dry.$UNIT_N.$un"
    # DOGANY_CREW_TXN_PREVIEW (DGN-1517 spec v1 §2.3(b)): at T0-5 neither the
    # lease nor any staged generation exists yet (lease is written at 1669+,
    # prepare is Phase P below -- both AFTER this loop), so the crew-lockstep
    # guard's evidence class 3a is structurally unreachable here. This env
    # var is 3a's dry-run preview counterpart (3b) -- it never affects a real
    # (DRY=0) run's verdict (3b is DRY-only, guard-side).
    DOGANY_STRICT_GATES=1 DOGANY_CREW_TXN_PREVIEW="$cand_tag" \
    bash "$INSTALLER" "$slug" "$mroot" \
      --pack "$pid" --pack-dir "$pdir" --source-tag "$cand_tag" \
      ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
      --upgrade --no-start --no-state --dry-run \
      < /dev/null > "$dout" 2>&1 || drc=$?
    sed 's/^/[apply]     /' "$dout"
    if [[ "$drc" -ne 0 ]]; then
      _log "  크루 $crew_kit/$pid: BLOCKED -- T0 pack_install --dry-run FAIL ($mname rc=$drc) -- 진입 전 차단 (쓰기 0건, 브리지 무접촉)"
      return 1
    fi
    _log "  크루 $crew_kit/$pid: T0 dry-run PASS [$un/${member_n}] $mname"
  done

  # ---- T0-6: disk headroom for the shared-DB backup ----
  local db_kb avail_kb need_kb
  db_kb="$(du -k "$crew_db" 2>/dev/null | awk '{print $1}' | head -n1)"
  db_kb="${db_kb:-0}"
  avail_kb="$(df -Pk "$crew_home" 2>/dev/null | awk 'NR==2 {print $4}')"
  avail_kb="${avail_kb:-0}"
  need_kb=$((db_kb * 2 + 5120))
  _log "  크루 $crew_kit/$pid: 디스크 여유 검사 -- 공유 DB ${db_kb}KB, 필요 ${need_kb}KB, 가용 ${avail_kb}KB"
  if [[ "$avail_kb" -lt "$need_kb" ]]; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- 디스크 여유 부족 (가용 ${avail_kb}KB < 필요 ${need_kb}KB), fail-closed"
    return 1
  fi

  if [[ "$DRY" -eq 1 ]]; then
    if [[ -n "$fw_units" ]]; then
      local g_n=0
      for m in $group_members; do g_n=$((g_n + 1)); done
      _log "  크루 $crew_kit/$pid: (dry-run) 계획 -- 그룹 전 브리지 정지(${g_n}건) -> 공유 DB 백업 1건($crew_db.t0.*) -> pack_install --upgrade x${member_n} (단일 추출본) -> 착지 검증 x${member_n} -> 세대 활성화+레이아웃 전환 x${member_n} -> framework x${fw_n} (self-update.sh --no-restart, 계획 후보 핀 고정) -> 그룹 전 브리지 재시작(${g_n}건)"
      _log "  크루 $crew_kit/$pid: (dry-run) 그룹 내부 순서 = 팩 -> 프레임워크 (fw_reqframework_guard가 전진된 팩 집합을 보도록 -- fw 선적용은 게이트가 정당하게 차단)"
    else
      _log "  크루 $crew_kit/$pid: (dry-run) 계획 -- 전 멤버 정지(${member_n}건, plist ${total_plc}건) -> 공유 DB 백업 1건($crew_db.t0.*) -> pack_install --upgrade x${member_n} (단일 추출본) -> 착지 검증 x${member_n} -> 세대 활성화+레이아웃 전환 x${member_n} -> 전 멤버 재시작(${member_n}건)"
    fi
    _log "  크루 $crew_kit/$pid: (dry-run) 공유 DB 마이그레이션은 1개 DB에 1회 -- 첫 멤버가 user_version을 핀으로 전진, 이후 멤버는 cur==pin 무이동 통과"
    _log "  크루 $crew_kit/$pid: (dry-run) 적용 예정 -- 쓰기/정지/백업/재시작 실행 안 함"
    return 0
  fi

  # ---- T0-6b: consume the consent token -- the single-use boundary
  # (DGN-1506 item 2). Deleted immediately BEFORE the consented pause
  # (T0-7), so no outcome of the transaction -- completion, INCOMPLETE,
  # signal, SIGKILL -- can leave a token behind that authorizes a SECOND
  # pause; a retry after any stop goes back through the skill's one-question
  # resumed-flow lane. Consume re-verifies (expiry may have passed since the
  # T0-1c fail-fast check: T0-5 dry-runs take real time). Everything up to
  # here wrote nothing, so this refusal is still pre-entry.
  if ! consent_why="$(_crew_consent_consume "$crew_kit" "$consent_roots")"; then
    _gate_stop crew.consent "  크루 $crew_kit/$pid: BLOCKED -- 동의 토큰 소비 실패 ($consent_why). T0-1c 검증 이후 상태가 변했습니다 -- 유저의 예를 다시 받아 scripts/pack/crew_consent.sh record --kit $crew_kit 후 재시도 (진입 전 차단, 쓰기 0건, 브리지 무접촉)"
    return 1
  fi
  _log "  크루 $crew_kit/$pid: 동의 토큰 1회 소비 완료 -- 이 동의는 이 정지에만 유효하며 재사용 불가 (DGN-1506 2번)"

  # ---- T0-7: stop ALL member bridges (verified). Failure = pre-entry ABORT
  # with zero writes; already-stopped members stay down (conservative --
  # same direction as the slice-2 preflight policy; availability relaxation
  # is a separate decision).
  local stopped="" sm stop_n=0
  for m in $group_members; do stop_n=$((stop_n + 1)); done
  if [[ -n "$fw_units" ]]; then
    _log "  크루 $crew_kit/$pid: 그룹 전 브리지 정지 (${stop_n}건 -- 크루 ${member_n} + fw 단독 $((stop_n - member_n))) -- 트랜잭션 진입"
  else
    _log "  크루 $crew_kit/$pid: 크루 전 멤버 브리지 정지 (${member_n}건) -- 트랜잭션 진입"
  fi
  for m in $group_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    if ! _launchd_stop "$mroot"; then
      _log "  크루 $crew_kit/$pid: BLOCKED -- 멤버 $mname 정지 검증 실패. 크루 트랜잭션 진입 전 중단 (쓰기 0건 상태 -- 백업 포함 어떤 파일도 만들지 않음)"
      if [[ -n "$stopped" ]]; then
        # Zero writes happened, so these members carry no half-state: the
        # loop-end re-arm owes them their units back (DGN-1506 item 5). It
        # was a "manual restart" hint here until that owner existed.
        _log "  크루 $crew_kit/$pid: 이미 정지된 멤버는 이 실행이 루프 끝에서 재무장합니다 (쓰기 0건 -- 반쪽 상태 없음). 대상:"
        for sm in $stopped; do
          _log "    ${sm#*:}"
        done
      fi
      return 1
    fi
    stopped="${stopped:+$stopped }$m"
  done

  # ---- T0-8: loop-level shared-DB backup (quiesced, fixed naming) ----
  local cur_uv ts bak
  cur_uv="$(sqlite3 "$crew_db" 'PRAGMA user_version;' 2>/dev/null || echo 0)"
  ts="$(date +%Y%m%d-%H%M%S)"
  bak="$crew_db.t0.v${cur_uv}.bak-$ts"
  if ! sqlite3 "$crew_db" ".backup '$bak'"; then
    # Same shape as T0-7: the transaction never entered, so nothing is
    # half-applied and the loop-end re-arm (DGN-1506 item 5) owns the group.
    _log "  크루 $crew_kit/$pid: BLOCKED -- T0 공유 DB 백업 실패 ($bak). 인스턴스 쓰기 0건, 그룹 브리지는 이 실행이 루프 끝에서 재무장합니다. 대상:"
    for sm in $group_members; do
      _log "    ${sm#*:}"
    done
    return 1
  fi
  _log "  크루 $crew_kit/$pid: T0 공유 DB 백업 1건 -> $bak (user_version=$cur_uv)"

  # ---- DGN-1509 B2: pessimistic lease write BEFORE the shared DB is
  # touched. Placed HERE (after T0-7 stop + T0-8 backup succeed, right
  # before T1 first calls pack_install --upgrade) rather than literally
  # before T0-7 as spec §4's prose reads ("첫 mutation = T0-7 브리지
  # 정지부터") -- a deliberate, documented deviation, not an oversight:
  #   - the concrete B2 failure this closes is specifically "signal during
  #     T1..TN" (grill + spec §7 walk, both describe member migrate already
  #     in flight); a signal during T0-7/T0-8 with zero shared-DB writes is
  #     not the same hazard -- a stopped-but-unmigrated bridge is safe to
  #     re-arm, and the existing T0-7/T0-8 BLOCKED paths above already
  #     promise exactly that ("이미 정지된 멤버는... 재무장합니다").
  #     Moving the dirty-write earlier would silently break that promise
  #     for the common "one member won't stop" case to guard a rarer
  #     window that never touches the DB anyway.
  #   - spec §8 item 5 itself flags the exact boundary ("DIRTY 진입점의
  #     정확한 위치") as an open, self-graded-thin point ("반례 환영") --
  #     this is that counter-example, recorded here rather than silently
  #     picking a side.
  # Still satisfies the "no PREPARING/DIRTY sub-split" rule: ONE pessimistic
  # write, one boundary, no second state between "about to migrate" and
  # "migrating" -- only the boundary's position differs from the prose.
  if ! _crew_lease_write_dirty "$crew_kit" "$cand_tag" "$(printf '%s\n' $conf_members | sed 's/^[^:]*://')"; then
    _log "  크루 $crew_kit/$pid: BLOCKED -- lease 기록 실패 (위 crew-lease 로그 참조). 공유 DB는 아직 무변경, 그룹 브리지는 이 실행이 루프 끝에서 재무장합니다. 대상:"
    for sm in $group_members; do
      _log "    ${sm#*:}"
    done
    return 1
  fi
  _log "  크루 $crew_kit/$pid: lease DIRTY 기록 (fsync 완료) -- 신호 중단 시 재무장 보류의 디스크 근거 (DGN-1509 B2)"

  # ---- Phase P: prepare EVERY member's candidate generation BEFORE any
  # member's migrate (DGN-1517 spec v1 §2.3(a)). Moved OUT of the per-member
  # T1 loop below -- where it used to run member-by-member, interleaved with
  # that SAME member's own upgrade -- into its own independent loop, placed
  # AFTER the lease DIRTY write (T0-7/T0-8 already promise zero writes
  # before that point; prepare's own contract is a member-scoped
  # database/.gen/ write, pack_install.sh --prepare header) and BEFORE T1.
  # Reason: member #1's migrate needs EVERY sibling's staged generation
  # already on disk for the crew-lockstep guard's evidence class 3a
  # (pack_install.sh _kit_crew_lockstep_e1e2) to see it -- with prepare still
  # inside the T1 loop, member #1's migrate would run before member #2 had
  # prepared anything. A prepare failure here is this member's INCOMPLETE,
  # same as any other Phase-P/T1..TN step failing (this point on, the lease
  # is DIRTY and no per-member-only rollback exists, spec §3).
  un=0
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    slug="$(_conf_get "$mroot/.instance.conf" DOGANY_AGENT_NAME)"
    un=$((un + 1))
    dout="$TMP/crew.prepare.$UNIT_N.$un"; drc=0
    DOGANY_STRICT_GATES=1 bash "$INSTALLER" "$slug" "$mroot" \
      --pack "$pid" --pack-dir "$pdir" --source-tag "$cand_tag" \
      ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
      --prepare < /dev/null > "$dout" 2>&1 || drc=$?
    sed 's/^/[apply]     /' "$dout"
    if [[ "$drc" -ne 0 ]]; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "pack_install rc=$drc (--prepare, Phase P)" "$bak" "$group_members"
      return 2
    fi
    _log "  크루 $crew_kit/$pid: [P$un/${member_n}] $mname prepare PASS (database/.gen/$cand_tag 착지)"
  done

  # ---- T1..TN: install + activate every member, one at a time, from the
  # single extraction (DGN-1509 S5/S7 Phase 1/S5b -- "... migrate-once ->
  # activate -> verify"; prepare now runs in Phase P above, not here -- see
  # that loop's comment). Each member's upgrade and activate+layout run
  # back-to-back in the SAME iteration (not as separate whole-crew passes)
  # so a failure on member K lands exactly where every failure in this loop
  # has always landed: members 1..K-1 fully complete (installed AND
  # activated), member K named INCOMPLETE, members K+1..N never attempted --
  # the established whole-crew-stays-down contract, unchanged in shape.
  local iout irc rec
  un=0
  for m in $conf_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    slug="$(_conf_get "$mroot/.instance.conf" DOGANY_AGENT_NAME)"
    un=$((un + 1))

    iout="$TMP/crew.install.$UNIT_N.$un"; irc=0
    _log "  크루 $crew_kit/$pid: [T$un/${member_n}] $mname pack_install --upgrade 위임 (payload=$cand_tag, 단일 추출본)"
    DOGANY_STRICT_GATES=1 bash "$INSTALLER" "$slug" "$mroot" \
      --pack "$pid" --pack-dir "$pdir" --source-tag "$cand_tag" \
      ${CATALOG_ARGS[@]+"${CATALOG_ARGS[@]}"} \
      --upgrade --no-start --no-state \
      < /dev/null > "$iout" 2>&1 || irc=$?
    sed 's/^/[apply]     /' "$iout"
    if [[ "$irc" -ne 0 ]]; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "pack_install rc=$irc" "$bak" "$group_members"
      return 2
    fi
    # Belt 2 (kit path wording): rc-0 without the kit gate PASS line means
    # the fail-open WARN+skip path fired mid-run -- laundering refused.
    if ! _install_gate_passed "$iout"; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "pack_install rc=0인데 'kit: compat-lint PASS' 라인 부재 (fail-open 경로 의심, 세탁 거부)" "$bak" "$group_members"
      return 2
    fi
    rec="$(_installed_ver "$mroot/.instance.conf" "$pid")"
    _log "  크루 $crew_kit/$pid: $mname 착지 검증 -- DOGANY_PACKS 기록 '$rec' vs 후보 '$cand_ver'"
    if [[ "$rec" != "$cand_ver" ]]; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "적용 후 기록('$rec')이 후보('$cand_ver')와 불일치" "$bak" "$group_members"
      return 2
    fi
    # DGN-1456/1460: 착지 검증 2 -- 실물 증거. rc=0 + 장부 일치여도
    # delivery-complete receipt 가 후보 버전으로 VERIFIED 되지 않으면 크루
    # 전체 미완주 (receipt 없는 성공은 성공이 아님).
    ev="$(evidence_consume "$mroot" "$pid" "$cand_ver")"
    ev_v="${ev%%|*}"; ev_d="${ev#*|}"
    if [[ "$ev_v" != "VERIFIED" ]]; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "적용 후 설치 증거 $ev_v ($ev_d) -- receipt 미발급/불일치 (DGN-1460)" "$bak" "$group_members"
      return 2
    fi
    _log "  크루 $crew_kit/$pid: $mname 적용 후 설치 증거 VERIFIED ($ev_d)"

    # activate + layout-migrate (S7 Phase 1 / S5b): database/.gen/$cand_tag/
    # already holds byte-identical content to what --upgrade just wrote
    # live (same $pdir extraction) -- flip .gen-active to it and convert
    # this member's real files into the permanent generation-symlink
    # layout, so the NEXT crew transaction finds this member already on
    # that layout (branch1 skip, S5b §2.3) and can activate with a single
    # flip. NOT a fix for DGN-1436's KNOWN OVER-BLOCK guard (see this
    # function's header comment) -- purely additive bookkeeping on top of
    # the live delivery T1 above already made.
    arc=0
    _crew_activate_generation "$mroot" "$cand_tag" || arc=$?
    if [[ "$arc" -ne 0 ]]; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "generation activate 실패 (rc=$arc, 위 crew-activate 로그 참조)" "$bak" "$group_members"
      return 2
    fi
    lrc=0
    _crew_layout_migrate "$mroot" "$cand_tag" || lrc=$?
    if [[ "$lrc" -ne 0 ]]; then
      _crew_incomplete "$crew_kit" "$pid" "$mname" "layout migrate 실패 (rc=$lrc, 위 crew-layout 로그 참조)" "$bak" "$group_members"
      return 2
    fi
    _log "  크루 $crew_kit/$pid: $mname 세대 활성화 + 레이아웃 전환 완료 ($cand_tag)"
  done

  local end_uv
  end_uv="$(sqlite3 "$crew_db" 'PRAGMA user_version;' 2>/dev/null || echo '?')"
  _log "  크루 $crew_kit/$pid: 공유 DB user_version $cur_uv -> $end_uv (마이그레이션은 DB 1개에 1회 -- 첫 멤버만 전진, 이후 멤버 cur==pin 무이동 통과)"

  # ---- ATOMIC GROUP fw phase (slice 4): AFTER the pack landings so the
  # shipped fw_reqframework_guard inside update.sh evaluates the ADVANCED
  # pack set (fw-first would be blocked by the still-old installed range --
  # the very incompatibility that made this group atomic). Bridges are all
  # down; there is no live prefix. Any failure = the WHOLE group INCOMPLETE.
  if [[ -n "$fw_units" ]]; then
    local futo su frc
    for fu in $fw_units; do
      funame="${fu%%;*}"; fu_rest="${fu#*;}"; furoot="${fu_rest%%;*}"
      fu_rest="${fu_rest#*;}"; futo="${fu_rest#*;}"
      su="${SELFUPD_OVERRIDE:-$furoot/routines/self-update.sh}"
      frc=0
      _fw_payload "$funame" "$furoot" "$futo" "$su" || frc=$?
      if [[ "$frc" -ne 0 ]]; then
        _crew_incomplete "$crew_kit" "$pid" "framework $funame" "self-update 실패 또는 착지 검증 실패 (위 로그 참조)" "$bak" "$group_members"
        return 2
      fi
    done
  fi

  # DGN-1509 B2/§7: release the lease BEFORE restarting anything -- spec §7's
  # fixed order is "수선 -> lease 해제 -> 재무장" (repair -> lease release ->
  # rearm), never the reverse. Everything above this point has verified
  # completion (per-member install + evidence + fw phase all passed), so
  # this is the ONLY call site that clears rather than marks-incomplete.
  _crew_lease_clear "$crew_kit"

  # ---- T-end: restart the WHOLE group (crew members + fw-only roots) ----
  local r_ok_n=0 r_defer_n=0
  for m in $group_members; do
    mname="${m%%:*}"; mroot="${m#*:}"
    # DGN-1710: same deferral as _apply_unit -- a member a later plan line
    # stops again (e.g. its framework unit, now planned last) restarts once,
    # at the loop-end flush. Lease is already cleared above, so the flush's
    # lease read sees this transaction as done.
    if _touched_later "$mroot"; then
      DEFER_UNIT_ROOTS="${DEFER_UNIT_ROOTS:+$DEFER_UNIT_ROOTS }$mname/$pid:$mroot"
      r_defer_n=$((r_defer_n + 1))
      _log "  크루 $crew_kit/$pid: $mname 재시작 유예 -- 이후 계획 단위가 같은 인스턴스를 다시 정지함 (루프 끝 flush, 에이전트당 재시작 1회)"
      continue
    fi
    if _restart "$mroot"; then
      N_RESTART=$((N_RESTART + 1))
      r_ok_n=$((r_ok_n + 1))
    else
      N_RESTART_FAIL=$((N_RESTART_FAIL + 1))
      OVERALL_RC=2
      _log "  크루 $crew_kit/$pid: $mname 재시작 실패 -- 수동 재기동 필요: $MINT_RUN start --root $mroot"
    fi
  done
  if [[ -n "$fw_units" ]]; then
    _log "  크루 $crew_kit/$pid: 완주(ATOMIC GROUP) -- 팩 $inst_now -> $cand_ver + framework ${fw_n}건, 그룹 재시작 성공 ${r_ok_n}건$([[ "$r_defer_n" -gt 0 ]] && echo ", 재시작 유예 ${r_defer_n}건")"
  else
    _log "  크루 $crew_kit/$pid: 완주 -- $inst_now -> $cand_ver (멤버 ${member_n}건, 재시작 성공 ${r_ok_n}건$([[ "$r_defer_n" -gt 0 ]] && echo ", 재시작 유예 ${r_defer_n}건"))"
  fi
  return 0
}

# _apply_atomic <desc> <fw_items "name;root;from;to ..."> <pack_line_file>
# ONE atomic unit = the whole group (spec §2: the slice-3 T0~T-end procedure
# generalized to the atomic-group boundary -- no new transaction machinery).
# returns: 0 OK / 1 BLOCKED (pre-entry, zero writes) / 2 INCOMPLETE (every
# group bridge stays down).
_apply_atomic() {
  local desc="$1" fw_items="$2" pack_line="$3"
  local a_pid a_kind a_prov a_tag a_cver a_members fu funame furoot fu_rest futo

  a_pid="$(printf '%s' "$pack_line" | cut -f2)"
  a_kind="$(printf '%s' "$pack_line" | cut -f3)"
  a_prov="$(printf '%s' "$pack_line" | cut -f4)"
  a_tag="$(printf '%s' "$pack_line" | cut -f6)"
  a_cver="$(printf '%s' "$pack_line" | cut -f7)"
  a_members="$(printf '%s' "$pack_line" | cut -f8)"
  if [[ -n "$(printf '%s' "$pack_line" | cut -f9)" ]]; then
    _abort "ATOMIC GROUP [$desc]: 크루 버전 맞추기(dec-263) 라인은 ATOMIC 그룹 안에서 지원하지 않음 -- fail-closed"
  fi

  _log "  ATOMIC GROUP [$desc]: 원자 단위 진입 판정 -- 그룹 전체 = 최소 적용 단위"
  # Supported composition today: crew-scoped pack + fw items. Anything else
  # is structurally unproducible by the planner -- fail closed, never guess.
  if [[ "$a_kind" != "kit" && -z "$a_prov" ]]; then
    _abort "ATOMIC GROUP [$desc]: 크루 스코프 아닌 팩($a_pid, kind='$a_kind')이 원자 그룹에 포함 -- 지원 구성 밖 (단일 크루 팩 + framework만 지원), fail-closed"
  fi

  # Pre-entry fw checks (read-only; a BLOCKED fw member blocks the WHOLE
  # group before any stop -- the group is the unit, spec §2).
  for fu in $fw_items; do
    funame="${fu%%;*}"; fu_rest="${fu#*;}"; furoot="${fu_rest%%;*}"
    fu_rest="${fu_rest#*;}"; futo="${fu_rest#*;}"
    if ! _fw_precheck "$funame" "$furoot" "$futo"; then
      _log "  ATOMIC GROUP [$desc]: BLOCKED -- framework $funame 사전검사 실패 (그룹 전체 차단, 부분 적용 없음)"
      return 1
    fi
  done

  _apply_crew "$a_pid" "$a_kind" "$a_prov" "$a_tag" "$a_cver" "$a_members" "$fw_items"
}

# NOTE: parsing uses cut (not IFS=tab read) -- tab is an IFS whitespace
# class character in bash, so consecutive tabs around an EMPTY field (e.g.
# provides_kit='') would collapse and shift every later column.
IN_ATOMIC=0
A_DESC=""
A_FW_ITEMS=""
A_PACK_LINE=""
A_PACK_N=0
A_N=0
while IFS= read -r line; do
  PLAN_LINE_N=$((PLAN_LINE_N + 1))
  kind="$(printf '%s' "$line" | cut -f1)"
  f1="$(printf '%s' "$line" | cut -f2)"
  f2="$(printf '%s' "$line" | cut -f3)"
  f3="$(printf '%s' "$line" | cut -f4)"
  f4="$(printf '%s' "$line" | cut -f5)"
  f5="$(printf '%s' "$line" | cut -f6)"
  f6="$(printf '%s' "$line" | cut -f7)"
  f7="$(printf '%s' "$line" | cut -f8)"
  f8="$(printf '%s' "$line" | cut -f9)"

  # An open ATOMIC group accepts ONLY its member lines / terminator -- any
  # other kind mid-group means a truncated or interleaved emit: fail closed.
  if [[ "$IN_ATOMIC" -eq 1 ]]; then
    case "$kind" in
      ATOMIC-FW|ATOMIC-PACK|ATOMIC-END) : ;;
      *) _abort "plan emit: ATOMIC 그룹이 ATOMIC-END 없이 '$kind' 라인으로 이어짐 -- 불완전/구버전 emit, fail-closed" ;;
    esac
  fi

  case "$kind" in
    PLAN-EMIT-V1) continue ;;
    # DGN-1843: planner bookkeeping, never an apply unit (blocked items are
    # NOT applied; the planner rc gate above already refused a FAIL plan).
    # dec-263: CREW-SPLIT is the owner surface's wording input, same class.
    BLOCKED|PLAN-END|CREW-SPLIT) continue ;;
    FW)
      # Standalone framework atomic unit (slice 4): self-update delegation,
      # restart deferred to loop end (spec §2 rule 1).
      fw_name="$f1"; fw_root="$f2"; fw_from="$f3"; fw_to="$f4"
      UNIT_TARGET="$fw_to"
      UNIT_META="framework"$'\x1f'"$fw_name"$'\x1f'$'\x1f'"$fw_name:$fw_root"
      N_TARGET=$((N_TARGET + 1))
      UNIT_N=$((UNIT_N + 1))
      if [[ "$FAILFAST" -eq 1 ]]; then
        _log "  framework $fw_name: 미시도 -- 선행 원자 단위 미완주로 이후 적용 중단 (정지는 해당 단위에만 유지, 완주분 재시작은 유효)"
        N_UNTRIED=$((N_UNTRIED + 1)); _unit_rec "framework $fw_name" untried
        continue
      fi
      UNIT_DETAIL=""
      if _retiring_hold "framework $fw_name" "$fw_root"; then
        N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
        _unit_rec "framework $fw_name" blocked "$RETIRING_REASON"
        continue
      fi
      frc=0
      _apply_fw "$fw_name" "$fw_root" "$fw_from" "$fw_to" || frc=$?
      case "$frc" in
        0) N_OK=$((N_OK + 1)); _unit_rec "framework $fw_name" ok ;;
        3) N_OK=$((N_OK + 1)); _unit_rec "framework $fw_name" ok "updated (landed earlier in this same run)" ;;
        1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "framework $fw_name" blocked "$FW_BLOCK_REASON" ;;
        2) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; FAILFAST=1
           _unit_rec "framework $fw_name" incomplete
           _mark_down "$fw_root" ;;
      esac
      continue ;;
    ATOMIC)
      IN_ATOMIC=1; A_DESC="$f1"; A_FW_ITEMS=""; A_PACK_LINE=""; A_PACK_N=0; A_N=0
      continue ;;
    ATOMIC-FW)
      [[ "$IN_ATOMIC" -eq 1 ]] || _abort "plan emit: 그룹 밖 ATOMIC-FW 라인 -- 손상된 emit, fail-closed"
      A_FW_ITEMS="${A_FW_ITEMS:+$A_FW_ITEMS }$f1;$f2;$f3;$f4"
      A_N=$((A_N + 1))
      continue ;;
    ATOMIC-PACK)
      [[ "$IN_ATOMIC" -eq 1 ]] || _abort "plan emit: 그룹 밖 ATOMIC-PACK 라인 -- 손상된 emit, fail-closed"
      A_PACK_LINE="$line"
      A_PACK_N=$((A_PACK_N + 1))
      A_N=$((A_N + 1))
      continue ;;
    ATOMIC-END)
      [[ "$IN_ATOMIC" -eq 1 ]] || _abort "plan emit: 그룹 밖 ATOMIC-END 라인 -- 손상된 emit, fail-closed"
      IN_ATOMIC=0
      if [[ "$f1" != "$A_N" ]]; then
        _abort "plan emit: ATOMIC-END 멤버 수($f1) != 수신 라인 수($A_N) -- 그룹이 온전하게 도착하지 않음, fail-closed"
      fi
      if [[ "$A_PACK_N" -ne 1 || -z "$A_FW_ITEMS" ]]; then
        _abort "ATOMIC GROUP [$A_DESC]: 팩 ${A_PACK_N}건 + framework $([[ -n "$A_FW_ITEMS" ]] && echo 有 || echo 0건) -- 지원 구성 밖 (framework >=1 + 크루 팩 정확히 1), fail-closed"
      fi
      N_TARGET=$((N_TARGET + 1))
      UNIT_N=$((UNIT_N + 1))
      UNIT_TARGET="$(printf '%s' "$A_PACK_LINE" | cut -f7)"
      a_members="$(printf '%s' "$A_PACK_LINE" | cut -f8)"
      UNIT_META="kit"$'\x1f'"$(printf '%s' "$A_PACK_LINE" | cut -f2)"$'\x1f'"$(printf '%s' "$A_PACK_LINE" | cut -f6)"$'\x1f'"$a_members"
      if [[ "$FAILFAST" -eq 1 ]]; then
        _log "  ATOMIC GROUP [$A_DESC]: 미시도 -- 선행 원자 단위 미완주로 이후 적용 중단"
        N_UNTRIED=$((N_UNTRIED + 1)); _unit_rec "atomic group [$A_DESC]" untried
        continue
      fi
      UNIT_DETAIL=""
      a_roots=""
      for fu in $A_FW_ITEMS; do
        _fu_rest="${fu#*;}"
        a_roots="$a_roots ${_fu_rest%%;*}"
      done
      # shellcheck disable=SC2086
      if _retiring_hold "atomic group [$A_DESC]" $a_members $a_roots; then
        N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
        _unit_rec "atomic group [$A_DESC]" blocked "$RETIRING_REASON"
        a_bkit="$(printf '%s' "$A_PACK_LINE" | cut -f4)"
        [[ -n "$a_bkit" ]] || a_bkit="$(printf '%s' "$A_PACK_LINE" | cut -f2)"
        BLOCKED_KITS="$BLOCKED_KITS $a_bkit"
        continue
      fi
      arc=0
      _apply_atomic "$A_DESC" "$A_FW_ITEMS" "$A_PACK_LINE" || arc=$?
      case "$arc" in
        0) N_OK=$((N_OK + 1)); _unit_rec "atomic group [$A_DESC]" ok ;;
        1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "atomic group [$A_DESC]" blocked
           a_bkit="$(printf '%s' "$A_PACK_LINE" | cut -f4)"
           [[ -n "$a_bkit" ]] || a_bkit="$(printf '%s' "$A_PACK_LINE" | cut -f2)"
           BLOCKED_KITS="$BLOCKED_KITS $a_bkit" ;;
        2) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; FAILFAST=1
           _unit_rec "atomic group [$A_DESC]" incomplete
           for m in $a_members; do _mark_down "${m#*:}"; done
           for fu in $A_FW_ITEMS; do
             _fu_rest="${fu#*;}"
             _mark_down "${_fu_rest%%;*}"
           done ;;
      esac
      continue ;;
    PACK) : ;;
    *) _abort "plan emit 미지원 항목 '$kind' -- fail-closed" ;;
  esac

  pid="$f1"; p_kind="$f2"; p_provides="$f3"; p_inst="$f4"
  p_tag="$f5"; p_cver="$f6"; p_members="$f7"; p_sync="$f8"
  if [[ -n "$p_sync" && "$p_kind" != "kit" && -z "$p_provides" ]]; then
    _abort "plan emit: 크루 버전 맞추기(dec-263) 필드가 크루 스코프가 아닌 팩 '$pid' 라인에 있음 -- 손상된 emit, fail-closed"
  fi
  UNIT_TARGET="$p_cver"
  if [[ "$p_kind" == "kit" || -n "$p_provides" ]]; then
    UNIT_META="kit"$'\x1f'"$pid"$'\x1f'"$p_tag"$'\x1f'"$p_members"
  else
    UNIT_META="pack"$'\x1f'"$pid"$'\x1f'"$p_tag"$'\x1f'"$p_members"
  fi

  if [[ "$p_kind" == "kit" || -n "$p_provides" ]]; then
    # Crew-scoped item (slice 3): the WHOLE crew is ONE atomic unit.
    N_TARGET=$((N_TARGET + 1))
    UNIT_N=$((UNIT_N + 1))
    if [[ "$FAILFAST" -eq 1 ]]; then
      _log "  크루 ${p_provides:-$pid}/$pid: 미시도 -- 선행 원자 단위 미완주로 이후 적용 중단 (정지는 해당 단위에만 유지, 완주분 재시작은 유효)"
      N_UNTRIED=$((N_UNTRIED + 1)); _unit_rec "crew ${p_provides:-$pid}/$pid" untried
      continue
    fi
    UNIT_DETAIL=""
    # shellcheck disable=SC2086
    if _retiring_hold "crew ${p_provides:-$pid}/$pid" $p_members; then
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
      _unit_rec "crew ${p_provides:-$pid}/$pid" blocked "$RETIRING_REASON"
      BLOCKED_KITS="$BLOCKED_KITS ${p_provides:-$pid}"
      continue
    fi
    crc=0
    _apply_crew "$pid" "$p_kind" "$p_provides" "$p_tag" "$p_cver" "$p_members" "" "$p_sync" || crc=$?
    case "$crc" in
      0) N_OK=$((N_OK + 1)); _unit_rec "crew ${p_provides:-$pid}/$pid" ok ;;
      1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "crew ${p_provides:-$pid}/$pid" blocked
         BLOCKED_KITS="$BLOCKED_KITS ${p_provides:-$pid}"
         _log "  크루 ${p_provides:-$pid}/$pid: BLOCKED -- 이 kit 에 의존(requires_kit)하는 이후 팩 단위는 브리지를 건드리기 전에 BLOCKED-BY-DEPENDENCY 로 멈춥니다 (DGN-1682)" ;;
      2) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; FAILFAST=1
         _unit_rec "crew ${p_provides:-$pid}/$pid" incomplete
         for m in $p_members; do _mark_down "${m#*:}"; done ;;
    esac
    continue
  fi

  # Empty member set on an emitted PACK item: structurally impossible from
  # the planner (an OK item implies >=1 mounting instance) -- if it appears,
  # it must FAIL loudly, never contribute a silent zero (self-grill b).
  if [[ -z "${p_members// /}" ]]; then
    _log "  pack $pid: BLOCKED -- 계획 항목에 멤버 0건 (0건은 통과가 아니라 의심), fail-closed"
    N_TARGET=$((N_TARGET + 1)); N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
    _unit_rec "pack $pid" blocked
    continue
  fi

  # Registry repo + tag existence (extraction source).
  repo="$(_reg_repo "$pid")"
  if [[ -z "$repo" || ! -d "$repo" ]]; then
    _log "  pack $pid: BLOCKED -- 레지스트리 repo 미해석 ('$repo'), fail-closed"
    N_TARGET=$((N_TARGET + 1)); N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
    _unit_rec "pack $pid" blocked
    continue
  fi

  for m in $p_members; do
    mname="${m%%:*}"
    mroot="${m#*:}"
    UNIT_META="pack"$'\x1f'"$pid"$'\x1f'"$p_tag"$'\x1f'"$m"
    N_TARGET=$((N_TARGET + 1))
    UNIT_N=$((UNIT_N + 1))

    if [[ "$FAILFAST" -eq 1 ]]; then
      _log "  $mname/$pid: 미시도 -- 선행 원자 단위 미완주로 이후 적용 중단 (정지는 해당 단위에만 유지, 완주분 재시작은 유효)"
      N_UNTRIED=$((N_UNTRIED + 1)); _unit_rec "$mname/$pid" untried
      continue
    fi
    UNIT_DETAIL=""; UNIT_REFUSAL=""
    if _retiring_hold "$mname/$pid" "$mroot"; then
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
      _unit_rec "$mname/$pid" blocked "$RETIRING_REASON"
      continue
    fi

    # Fresh extraction per unit (immutable tag archive -> private tmp).
    pdir="$TMP/payload.$UNIT_N"
    mkdir -p "$pdir"
    if ! git -C "$repo" rev-parse -q --verify "refs/tags/$p_tag" >/dev/null 2>&1; then
      _log "  $mname/$pid: BLOCKED -- 후보 태그 $p_tag 이 repo($repo)에 없음 (계획-적용 창 사이 변동 의심), fail-closed"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "$mname/$pid" blocked
      continue
    fi
    if ! git -C "$repo" archive --format=tar "refs/tags/$p_tag" | tar -xf - -C "$pdir"; then
      _log "  $mname/$pid: BLOCKED -- 태그 $p_tag payload 추출 실패, fail-closed"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "$mname/$pid" blocked
      continue
    fi
    # Extracted-manifest asserts (id poisoning + crew-scope defense in depth).
    mfline="$("$PACK_PY3" -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
def s(k):
    v = d.get(k)
    return v if isinstance(v, str) else ''
rk = d.get('requires_kit')
rk = rk.get('kit') if isinstance(rk, dict) else ''
rk = rk if isinstance(rk, str) else ''
print('\t'.join([s('id'), s('kind'), s('pack_version'), s('provides_kit'), rk]))
" "$pdir/pack-manifest.json" 2>/dev/null || true)"
    mf_id="$(printf '%s' "$mfline" | cut -f1)"
    mf_kind="$(printf '%s' "$mfline" | cut -f2)"
    mf_ver="$(printf '%s' "$mfline" | cut -f3)"
    mf_prov="$(printf '%s' "$mfline" | cut -f4)"
    mf_rkit="$(printf '%s' "$mfline" | cut -f5)"
    if [[ -z "$mfline" || "$mf_id" != "$pid" ]]; then
      _log "  $mname/$pid: BLOCKED -- 추출 manifest id('$mf_id') != 레지스트리 id('$pid') -- 구독원 오염 의심, fail-closed"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "$mname/$pid" blocked
      continue
    fi
    if [[ "$mf_kind" == "kit" || -n "$mf_prov" ]]; then
      _log "  $mname/$pid: BLOCKED -- 계획은 인스턴스 스코프인데 추출본이 크루 스코프 (kind='$mf_kind' provides_kit='$mf_prov') -- 계획-추출 불일치, 크루 트랜잭션 없이 적용하지 않음 (fail-closed)"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "$mname/$pid" blocked
      continue
    fi
    # 3-point check, pre-release form (dsp t2pin): a pre-release tag whose
    # manifest still carries the base version (v0.11.0-dev.1 over
    # pack_version=0.11.0) is the sanctioned dogfooding shape -- the tag may
    # ADD a -pre/+build suffix to the manifest version, nothing else.
    if [[ -n "$mf_ver" && "$mf_ver" != "$p_cver" \
          && "$p_cver" != "$mf_ver"-* && "$p_cver" != "$mf_ver"+* ]]; then
      _log "  $mname/$pid: BLOCKED -- 추출 manifest pack_version($mf_ver) != 태그 버전($p_cver) (pre-release 접미 초과 발산), 3점 불일치 fail-closed"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "$mname/$pid" blocked
      continue
    fi

    # DGN-1682 item 3: dependency stop BEFORE _apply_unit (which is where
    # the bridge is stopped). The kit this candidate requires was BLOCKED
    # earlier in this run, so installing it here can only fail its
    # requires_kit gate with the bridge already down. Not attempted, bridge
    # untouched, counted under 차단 and named in the notice.
    if [[ -n "$mf_rkit" && " $BLOCKED_KITS " == *" $mf_rkit "* ]]; then
      _log "  $mname/$pid: BLOCKED-BY-DEPENDENCY -- 후보 $p_tag 가 requires_kit '$mf_rkit' 를 요구하는데 이번 실행에서 그 kit 크루 단위가 BLOCKED. 브리지 정지 전에 멈춤 (정지 0건, 쓰기 0건). kit 크루 단위를 먼저 해소한 뒤 다시 실행하세요 (DGN-1682)"
      N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2
      N_BLOCK_DEP=$((N_BLOCK_DEP + 1))
      _unit_rec "$mname/$pid" blocked "not changed: it needs $mf_rkit, which could not be updated in this run; this agent was not touched" "$mf_rkit"
      BLOCK_DEP_UNITS="${BLOCK_DEP_UNITS:+$BLOCK_DEP_UNITS, }$mname/$pid(<-$mf_rkit)"
      continue
    fi

    urc=0
    _apply_unit "$pid" "$mroot" "$mname" "$p_tag" "$p_cver" "$pdir" || urc=$?
    case "$urc" in
      0) N_OK=$((N_OK + 1)); _unit_rec "$mname/$pid" ok ;;
      1) N_BLOCK=$((N_BLOCK + 1)); OVERALL_RC=2; _unit_rec "$mname/$pid" blocked ;;
      2) N_INCOMPLETE=$((N_INCOMPLETE + 1)); OVERALL_RC=2; FAILFAST=1
         _unit_rec "$mname/$pid" incomplete
         _mark_down "$mroot" ;;
    esac
  done
done < <(tail -n +1 "$PLAN_TSV")

if [[ "$IN_ATOMIC" -eq 1 ]]; then
  _abort "plan emit: ATOMIC 그룹이 ATOMIC-END 없이 파일이 끝남 -- 불완전 emit, 부분 그룹으로는 절대 적용하지 않습니다 (fail-closed)"
fi

# ---------------------------------------------------------------------------
# 3) Loop-end: deferred framework restarts (spec §2 rule 1 -- fw restarts
#    join the atomic-unit policy at the loop boundary; completed units only),
#    then the stop debt: any root this run took units away from and never
#    restarted gets them back here (DGN-1506 item 5). Same boundary, same
#    stay-down policy -- an INCOMPLETE unit's root is skipped by both.
# ---------------------------------------------------------------------------
_flush_deferred
_rearm_stopped

# ---------------------------------------------------------------------------
# 4) Completion result -- exactly ONE delivery per run (dec-121 direction).
#    DGN-1709: a structured record to the owning agent (who tells the owner
#    in its own words), neutral push only without an agent path -- see
#    _deliver_result. The outcome can never read "done" unless EVERY target
#    unit completed (_apply_outcome); an INCOMPLETE unit, a BLOCKED unit or
#    a failed restart always surfaces.
# ---------------------------------------------------------------------------
_notify() {
  if [[ "$N_TARGET" -eq 0 ]]; then
    # DGN-1417: a ledger run still owes its supervisor a record -- 0 units
    # touched reads "blocked" (nothing changed), never silence.
    if [[ -n "${RESULT_FILE_PRODUCT:-}" && "$DRY" -eq 0 ]]; then
      _deliver_result "완료" "blocked"
      return 0
    fi
    _log "통지 생략 -- 적용 대상 0건 (통지할 착지/중단 없음. 0건 사유는 상단에 명시됨)"
    return 0
  fi
  local outcome
  outcome="$(_apply_outcome)"
  if [[ "$DRY" -eq 1 ]]; then
    _log "(dry-run) 결과 기록 1건 예정 (outcome=$outcome) -- 전달 안 함. 기록:"
    _result_record "$outcome" | sed 's/^/[apply]     /'
    # Route preview: --dry-run must show WHERE the record would go (read-only
    # scan) so a live rehearsal can verify the owning instance resolves.
    _resolve_notify_push
    if _agent_path_ok; then
      _log "(dry-run) 전달 예정: 소유 에이전트 세션 inbox ($NOTIFY_ROOT/.telegram_bot/session-inbox)"
    elif [[ -n "$NOTIFY_PUSH" ]]; then
      _log "(dry-run) 에이전트 경로 없음 (${AGENT_PATH_WHY}) -- 중립 최소 통지 예정, 발신자: $NOTIFY_PUSH (슬러그 '$NOTIFY_SLUG')"
    else
      _log "(dry-run) 에이전트 경로 없음 (${AGENT_PATH_WHY}), 발신자 해소도 실패 -- ${NOTIFY_PUSH_WHY}. 실전달 시엔 WARN + 본문 로그로 남습니다 (대리 발송 없음)"
    fi
    return 0
  fi
  _deliver_result "완료" "$outcome"
  return 0
}
_notify

# ---------------------------------------------------------------------------
# 5) §6 accounting -- counts always printed; 0 targets get loud reasons.
# ---------------------------------------------------------------------------
_log ""
if [[ "$DRY" -eq 1 ]]; then
  _log "== 결과 (dry-run -- 실행 0건) =="
else
  _log "== 결과 =="
fi
_log "적용 대상 ${N_TARGET}건 / 적용 성공 ${N_OK}건 / 차단 ${N_BLOCK}건 / 미완주(정지 유지) ${N_INCOMPLETE}건 / 미시도 ${N_UNTRIED}건 / 범위외 스킵 ${N_SKIP}건 / 재시작 ${N_RESTART}건"
if [[ "$N_RESTART_FAIL" -gt 0 ]]; then
  _log "재시작 실패 ${N_RESTART_FAIL}건 -- 수동 재기동 필요 (위 로그 참조)"
fi
if [[ "${N_STOP_FAIL:-0}" -gt 0 ]]; then
  _log "정지 검증 실패(재확인 필요) ${N_STOP_FAIL}건 -- 차단 집계에 포함, 위 로그에서 대상 확인 (DGN-1511/DGN-1640)"
fi
if [[ "$N_BLOCK_DEP" -gt 0 ]]; then
  _log "BLOCKED-BY-DEPENDENCY ${N_BLOCK_DEP}건 -- 차단 집계에 포함, 브리지 정지 0건: ${BLOCK_DEP_UNITS} (DGN-1682)"
fi
if [[ "$N_TARGET" -eq 0 ]]; then
  if [[ "$PLAN_ITEMS" -eq 0 ]]; then
    _log "적용 대상 0건 -- 계획 자체가 0건 (플래너 차단/보류 사유는 상단 출력에 명시됨. 0건은 통과가 아니라 의심)"
  else
    _log "적용 대상 0건 -- 계획 ${PLAN_ITEMS}건이 전부 범위외 스킵(${N_SKIP}건). 항목별 사유는 위에 명시됨 (조용한 통과 아님)"
  fi
fi
if [[ "$((N_OK + N_BLOCK + N_INCOMPLETE + N_UNTRIED))" -ne "$N_TARGET" ]]; then
  _abort "카운트 정합 실패: 성공+차단+미완주+미시도(${N_OK}+${N_BLOCK}+${N_INCOMPLETE}+${N_UNTRIED}) != 대상 ${N_TARGET} -- FATAL"
fi
if [[ "$N_INCOMPLETE" -gt 0 ]]; then
  _log "미완주 ${N_INCOMPLETE}건: 해당 인스턴스 브리지는 정지 유지 상태입니다. 반쪽 상태로 라이브 복귀하지 않습니다."
fi

# Normal completion reached: the EXIT trap must NOT fire the abort notice
# (a nonzero OVERALL_RC here was already surfaced by the completion notice).
RUN_COMPLETED=1
exit "$OVERALL_RC"
