# mint_teardown.sh -- the way back out of a mint that stopped midway (DGN-1506).
# Sourced library (no shebang for the sourced half, skill_retire.sh precedent).
#
# THE GAP THIS CLOSES: mint_run.sh exposed
#   {mint|start|pipeline|recover|stamp-role|align-peer-main}
# and NOT ONE of those removes anything. `recover` replays post-mint stamps
# from the journal -- it is a forward verb, not a way back. So a mint that
# stops midway (measured 2026-09-16: the DGN-1461 requires_kit scope gate --
# the PLANNED step, not a fault) leaves a half-minted instance on disk with
# no scripted way to undo it. On 2026-09-16 that residue (498MB) was removed
# BY HAND. Owner ruling the same day, 12:08: "if 'someone does it by hand'
# appears in a solution, it is not a solution" -- an outside user has no dev
# crew and no hand. Hence a VERB, not a documented procedure.
#
# THE ONE THING IT MUST NEVER DO is eat a live agent. Two independent guards,
# both fail-closed, both reading MACHINE FACTS only (DGN-1506 proposition:
# a forcing point whose input is not a machine fact is not a forcing point):
#
#   GUARD 1 -- LIVENESS (refuse if ANY hit, exit 3). Never inferred from
#   prose or from a caller's promise:
#     a. a launchd unit carrying one of THIS instance's own plist labels is
#        registered (launchctl print, READ-ONLY -- this library never boots
#        out, loads, stops or reconfigures anything; see BOUNDARY below);
#     b. a running process whose command line contains the instance root
#        (own process ancestry excluded -- our own argv carries the root);
#     c. a bridge-runtime artifact was touched within MINT_TEARDOWN_FRESH_S
#        (see MINT_TEARDOWN_RUNTIME_MARKS). These files are written by the
#        BRIDGE, never by mint -- measured 2026-09-16 on a real
#        `scripts/mint.sh --no-venv` mint (0/5 present) against two live
#        instances (5/5 present each).
#
#   The EXISTENCE of those same artifacts is a separate question from their
#   freshness, and the two gates split it deliberately: fresh = "may still be
#   running" (guard 1); merely present = "has run before" (guard 2). Without
#   the split, a live agent whose units were just booted out -- exactly the
#   DGN-1506 item 4/5 window -- would be refused for the wrong reason, and
#   the refusal message is the only thing a caller has to act on.
#
#   GUARD 2 -- POSITIVE EVIDENCE that this really is an unfinished mint
#   (refuse if neither holds, exit 4). Absence of liveness alone is NOT
#   enough: a live agent whose units were just booted out (exactly the
#   DGN-1506 item 4/5 window) is also "not currently running".
#     TXN      <root>/.mint-incomplete exists -- the transaction marker
#              mint_run.sh opens when it starts writing an instance and
#              removes only when the mint (or the whole pipeline) finishes.
#              Machine-written, machine-cleared; a completed agent never
#              carries it.
#     NEVER-RAN  the instance carries .instance.conf but not one bridge
#              runtime artifact -- it was born and never woke up. This is the
#              branch that covers mints broken BEFORE the marker existed
#              (today's residue), and the shape actually measured on
#              2026-09-16 (launchd never registered, .telegram_bot holding
#              logs/ + state/ only).
#
#   FLOW     (DGN-1683 grill fix 8) a wrapper-owned FLOW RECEIPT names this
#            root and the mint flow that is being aborted right now
#            (MINT_TEARDOWN_FLOW_RECEIPT=<file>, verified against the ordering
#            instance's config/mint-flow.json by lib/mint_flow_guard.py:
#            receipt root == this root, flow_id equal, flow state
#            ABORT_PENDING|CLEANING, slug equal). `pipeline --no-start` closes
#            .mint-incomplete before it exits, so from that point the marker
#            can no longer prove "unfinished"; the flow receipt does. It is a
#            SECOND evidence kind, never a forged marker. When the receipt
#            also records newborn_stopped (the flow itself stopped the unit it
#            started -- owner ruling dec-174, 2026-09-24) guard 1c alone is
#            waived: the artifacts are then the dead newborn's own last
#            writes. Guard 1a (registered unit) and 1b (live process) are NOT
#            waived and are re-checked here.
#
# BOUNDARY (deliberate, not an oversight): teardown does NOT bootout, unload,
# stop or start anything. A registered unit means the OS still considers that
# agent alive, and stopping a live bridge is an owner-gated service op
# (rules/hot.framework.md, Machine safety: "Bots/bridge: NEVER auto
# restart/stop/reconfigure"). Giving teardown the power to bootout would be
# giving it exactly the power to eat a live agent. So a still-registered
# instance is REFUSED with its blocking labels named; once those units are
# booted out through the normal owner-gated path, teardown proceeds on its
# own -- the automation stops at the owner gate, never at a human `rm`.
#
# NOTHING IS DELETED. Every byte is MOVED under
#   <archive-root>/<name>.<UTC ts>/
# ("trash > rm; reversible wins"), and RECEIPT.json records every move so
# mint_teardown_restore can put it all back. In particular the instance
# .env holds a LIVE BOT TOKEN the user obtained from BotFather by hand:
# deleting the directory would silently destroy a credential that is (1) not
# ours to destroy, (2) needed to retry the very mint that failed, and (3)
# revocable only through the user's own BotFather auth, which no script may
# drive. So the token travels into the archive intact and the receipt says
# so in a machine-readable field (bot_token_present) instead of a human
# remembering to look.
#
# ONE deliberate exception to "moved, never removed" (DGN-1543 R8): the
# instance's member=<root> rows in $CREW_DIR/<kit>/crew.conf. A registry row
# pointing at an archived path is not data, it is a false statement other
# machinery fail-closes on (update_plan T0-1 cross-validation) -- measured
# three times as a hand-deleted ghost. Apply removes each row through
# crew_leave.sh's verb (crew.conf backed up first, backup path in the
# receipt), and restore re-adds the exact stored line.

# DGN-1424: PATH-independent python3 resolver (see py_resolve.sh header).
_MT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=py_resolve.sh
. "$_MT_LIB_DIR/py_resolve.sh"

# DGN-1543 R8: the crew leave verb. Teardown archives the instance body and
# then DEREGISTERS it from every crew.conf that listed it -- an archived root
# left registered is exactly the ghost-member defect measured three times
# (crew.conf.bak-wargremove / -warg2 / -healthremove hand edits). Predicate,
# backup convention and refusal lanes live in crew_leave.sh; this file only
# calls them, so the manual verb and the teardown wiring cannot drift.
# shellcheck source=crew_leave.sh
. "$_MT_LIB_DIR/crew_leave.sh"

# Bridge-runtime artifacts: written by the bridge itself, never by mint.
# Measured 2026-09-16 (see header). Keep the list additive -- a new entry can
# only make the gate MORE reluctant, which is the safe direction.
MINT_TEARDOWN_RUNTIME_MARKS=(
  ".telegram_bot/logs/bot.log"
  ".telegram_bot/poll_heartbeat"
  ".telegram_bot/sessions.json"
  ".telegram_bot/runtime-snapshot.json"
  ".telegram_bot/.service_plist"
)

MINT_TEARDOWN_TXN_FILE=".mint-incomplete"

# DGN-1646: this is the one transcript-read-key predicate.  Occupancy and
# both mint entry paths call it; do not duplicate its filesystem test here.
_mt_transcript_clean() {
  pack_resolve_py3 || return 1
  "$PACK_PY3" "$_MT_LIB_DIR/transcript_store.py" "$1"
}

_mt_transcript_archive() { # <root> <archive/transcripts>
  pack_resolve_py3 || return 1
  "$PACK_PY3" "$_MT_LIB_DIR/transcript_store.py" archive "$1" "$2"
}

_mt_transcript_list() { # <root>
  pack_resolve_py3 || return 1
  "$PACK_PY3" "$_MT_LIB_DIR/transcript_store.py" list "$1"
}

_mt_transcript_restore() { # <archive/transcripts>
  pack_resolve_py3 || return 1
  "$PACK_PY3" "$_MT_LIB_DIR/transcript_store.py" restore "$1"
}

_mt_transcript_restore_check() { # <archive/transcripts>
  pack_resolve_py3 || return 1
  "$PACK_PY3" "$_MT_LIB_DIR/transcript_store.py" check-restore "$1"
}

# A stopped mint can create a transcript before it creates an instance body.
# It is its own reversible residue, not a predecessor the owner must tear down.
mint_transcript_clear_aborted() { # <root> <archive-root>
  local root="$1" archive_root="$2" archive base n=0 names
  [[ ! -e "$root" ]] || return 1
  _mt_transcript_clean "$root" && return 0
  base="$archive_root/mint-residue-$(basename "$root").$(_mt_ts)"
  archive="$base"
  while [[ -e "$archive" ]]; do n=$((n + 1)); archive="$base-$n"; done
  mkdir -p "$archive" || return 1
  names="$(_mt_transcript_archive "$root" "$archive/transcripts")" || return 1
  printf '{\n  "ticket": "DGN-1646",\n  "kind": "aborted-mint-transcripts",\n  "original_root": "%s",\n  "restore": "move transcripts/ back to ~/.claude/projects/",\n  "transcript_dirs": [%s]\n}\n' \
    "$root" "$(printf '%s\n' "$names" | "$PACK_PY3" -c 'import json,sys; print(", ".join(json.dumps(x.rstrip()) for x in sys.stdin if x.rstrip()))')" > "$archive/RECEIPT.json"
  echo "[preflight] archived aborted-mint transcript residue -> $archive/transcripts" >&2
}

# Test seams: the two OS probes are the only non-filesystem inputs, so they
# are the only things a test cannot set up on disk. Both default to the real
# binary; the suite points them at stubs.
_mt_launchctl() { "${MINT_TEARDOWN_LAUNCHCTL:-launchctl}" "$@"; }
_mt_ps()        { "${MINT_TEARDOWN_PS:-ps}" "$@"; }
# Freshness window for guard 1c, seconds. One hour: the bridge writes
# poll_heartbeat continuously while it runs, so anything older than that is
# not a running bridge. Overridable for tests only -- widening it can only
# make the gate MORE reluctant, never less.
MINT_TEARDOWN_FRESH_S="${MINT_TEARDOWN_FRESH_S:-3600}"

_mt_mtime() { # _mt_mtime <file> -- epoch seconds, empty when unavailable
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || true
}

_mt_phys() { # _mt_phys <path> -- physical path, empty when unresolvable
  local p="$1"
  [[ -n "$p" ]] || return 0
  if [[ -d "$p" ]]; then (cd "$p" 2>/dev/null && pwd -P) || true
  else printf '%s' "$p"; fi
}

_mt_ts() { date -u +%Y%m%dT%H%M%SZ; }

_mt_launchagents_dir() { printf '%s\n' "${DOGANY_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"; }

# _mt_launchagent_paths <root> -- installed plist copies owned by this root,
# one absolute path per line.  This calls mint_retire's shared inventory: the
# install side copies by filename, so a Label-derived `<label>.plist` guess
# misses e.g. `com.telegram-skill-bot.<slug>.newbridge.plist`.
_mt_launchagent_paths() {
  local root="$1"
  pack_resolve_py3 || return 1
  "$PACK_PY3" - "$_MT_LIB_DIR" "$root" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from mint_retire import installed_plist_paths
for path in installed_plist_paths(sys.argv[2]):
    print(path)
PYEOF
}

# DGN-1616: teardown tells the health observer what it removed. Without a
# `retired` marker the observer reports every booted-out label of this
# instance as a silent death -- same sentence, same button as a real one --
# and an alarm that is usually "that was us" gets learned away. Rationale and
# timing facts: the DGN-1616 design record. Mechanism is mint_retire's marker
# writer (write + readback); this file only calls it.
#
# _mt_observer_known <root> -- read-only. Prints "RETIRE <label>" for every
# label of this root's plist inventory that the observer state knows, and
# "ALERTED <label> <ts>" when its loss alarm already fired. Unknown labels are
# skipped: the observer never saw them, so they cannot raise a loss event.
_mt_observer_known() {
  local root="$1"
  pack_resolve_py3 || return 1
  "$PACK_PY3" - "$_MT_LIB_DIR" "$root" <<'PYEOF'
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import mint_retire as m
root = Path(sys.argv[2])
try:
    labels = sorted(m.plist_inventory(root))
    path = Path(os.environ.get('DOGANY_HEALTH_OBSERVER_STATE_DIR',
                               str(Path.home() / '.dogany/health-observer'))) / 'jobs.json'
    if not path.exists():
        sys.exit(0)
    jobs = m.observer_load(path)['jobs']
except m.Refusal as e:
    print(e.detail, file=sys.stderr)
    sys.exit(1)
for label in labels:
    entry = jobs.get(label)
    if not isinstance(entry, dict):
        continue
    print('RETIRE ' + label)
    if entry.get('lost_notified_at'):
        print('ALERTED %s %s' % (label, entry['lost_notified_at']))
PYEOF
}

# _mt_observer_mark <root> <out-json> <tx> <why> <label>... -- writes the
# markers and READS THEM BACK (the write's own exit is not the proof). rc 0 =
# every marker observed; rc 1 = not confirmed (best-effort rollback done).
_mt_observer_mark() {
  local root="$1" out="$2" tx="$3" why="$4"; shift 4
  pack_resolve_py3 || return 1
  "$PACK_PY3" - "$_MT_LIB_DIR" "$root" "$out" "$tx" "$why" "$@" <<'PYEOF'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import mint_retire as m
root, out, tx, why, labels = Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6:]
try:
    r = {'observer': m.observer_plan(root, labels, tx, why)}
    m.write_markers(r)
except (m.Refusal, OSError, ValueError) as e:
    print('observer marker write failed: %s' % e, file=sys.stderr)
    sys.exit(1)
with open(out, 'w', encoding='utf-8') as f:
    json.dump(r['observer'], f, indent=2)
    f.write('\n')
if r['observer']['status'] != 'complete':
    print('observer readback missing: ' + ' '.join(r['observer'].get('missing', [])), file=sys.stderr)
    try:
        m.restore_markers(r)
    except Exception as e:
        print('observer rollback failed: %s' % e, file=sys.stderr)
    sys.exit(1)
PYEOF
}

# DGN-1615 axis 2: a removal verb's exit code does not tell success from a
# no-op (`launchctl remove <wrong label>` and the old `--retire <any string>`
# both exit 0; measured 2026-09-20/21). Teardown therefore never reports a
# step done on the command's exit alone -- each one is re-OBSERVED.
# _mt_verify_gone <what> <src> <dst> -- rc 0 when <src> is gone AND <dst>
# exists; otherwise a loud ERROR line on stderr and rc 1.
_mt_verify_gone() {
  local what="$1" src="$2" dst="$3"
  if [[ -e "$src" || -L "$src" ]]; then
    echo "[teardown] ERROR: $what still present after the move: $src" >&2; return 1
  fi
  if [[ ! -e "$dst" ]]; then
    echo "[teardown] ERROR: $what not found at its archive destination: $dst" >&2; return 1
  fi
}

# _mt_bootout_targets <live-lines> -- one "bootout-target gui/<uid>/<label>"
# line per "LIVE launchd-unit <label>" hit. The target is the plist's Label
# (mint_teardown_labels), never a file name -- `...newbridge.plist` is
# Label `...` (DGN-1615). Callers print these so the owner-gated bootout
# uses the machine's list instead of re-deriving one.
_mt_bootout_targets() {
  local uid; uid="$(id -u)"
  printf '%s\n' "$1" | awk -v uid="$uid" '$1=="LIVE"&&$2=="launchd-unit"&&$3!="cannot-determine-label"{print "bootout-target gui/" uid "/" $3}'
}

# mint_teardown_labels <root> -- launchd labels this instance owns, one per
# line. SAME two globs `mint_run.sh start` loads from, so teardown can never
# judge a different unit set than the one start created.
mint_teardown_labels() {
  local root="$1" p
  for p in "$root"/bridge/*.plist "$root"/routines/*.plist; do
    [[ -e "$p" ]] || continue
    plist_label "$p" || return 1
  done
}

# _mt_kin <pid> <ppid-map> -- rc 0 when <pid> is US: either an ancestor of
# this script (the caller's own command line repeats --root <root>) or a
# descendant of it (every $( ) subshell and process substitution this
# function itself spawns). Measured need: without the DESCENDANT half the
# probe reported four of its own subshells as live bridges, because $$ stays
# the outer shell's pid inside a subshell -- the subshells are children of
# $$, never ancestors, so an ancestors-only walk misses them entirely.
# <ppid-map> is a single " pid:ppid ..." snapshot, so ancestry costs no extra
# ps calls (a live estate has ~700 processes).
_mt_kin() {
  local pid="$1" map="$2" n=0 cur
  # ancestors of $$ (and $$ itself)
  cur=$$
  while [[ "$cur" -gt 1 && "$n" -lt 32 ]]; do
    [[ "$cur" == "$pid" ]] && return 0
    cur="${map##* $cur:}"; cur="${cur%% *}"
    [[ -n "$cur" && "$cur" != "$map" ]] || break
    n=$((n + 1))
  done
  # ancestors of the candidate -- do we reach $$?
  cur="$pid"; n=0
  while [[ "$cur" -gt 1 && "$n" -lt 32 ]]; do
    [[ "$cur" == "$$" ]] && return 0
    cur="${map##* $cur:}"; cur="${cur%% *}"
    [[ -n "$cur" && "$cur" != "$map" ]] || break
    n=$((n + 1))
  done
  return 1
}

# mint_teardown_liveness <root> -- prints one "LIVE <kind> <detail>" line per
# hit. rc 0 = at least one hit (REFUSE), rc 1 = clean.
# READ-ONLY: launchctl print / ps only.
mint_teardown_liveness() {
  local root="$1" waive_artifacts="${2:-0}" hit=0 label mark pid cmd line
  # (locals for the process and freshness probes are declared at use site)
  local uid; uid="$(id -u)"

  local labels
  if ! labels="$(mint_teardown_labels "$root")"; then
    echo "LIVE launchd-unit cannot-determine-label (fail-closed)"; hit=1
    labels=""
  fi
  while IFS= read -r label; do
    [[ -n "$label" ]] || continue
    if _mt_launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
      echo "LIVE launchd-unit $label"; hit=1
    fi
  done <<< "$labels"

  # ONE ps snapshot feeds both the match and the kinship walk.
  local snap ppid_map root_phys
  root_phys="$(_mt_phys "$root")"
  snap="$(_mt_ps -Ao pid=,ppid=,command= 2>/dev/null | sed 's/^ *//')"
  ppid_map=" $(printf '%s\n' "$snap" | awk '{print $1":"$2}' | tr '\n' ' ')"
  while IFS= read -r line; do
    pid="${line%% *}"; line="${line#* }"
    line="${line#"${line%%[![:space:]]*}"}"
    cmd="${line#* }"
    [[ -n "$pid" ]] || continue
    # Match the root as given AND as resolved: on macOS a $TMPDIR path is
    # /var/folders/... while pwd -P yields /private/var/folders/..., and a
    # process started through one spelling must not hide from the other.
    case "$cmd" in
      *"$root"*) ;;
      *) if [[ -n "$root_phys" && "$root_phys" != "$root" && "$cmd" == *"$root_phys"* ]]; then
           :
         else
           continue
         fi ;;
    esac
    _mt_kin "$pid" "$ppid_map" && continue
    echo "LIVE process $pid ${cmd:0:120}"; hit=1
  done <<< "$snap"

  local now mt age
  now="$(date +%s)"
  for mark in "${MINT_TEARDOWN_RUNTIME_MARKS[@]}"; do
    # DGN-1683: the flow stopped its own newborn (receipt newborn_stopped);
    # freshness of the dead bridge's last writes no longer means "running".
    [[ "$waive_artifacts" == "1" ]] && continue
    [[ -e "$root/$mark" ]] || continue
    mt="$(_mt_mtime "$root/$mark")"
    # An unreadable mtime is treated as FRESH: fail-closed beats guessing.
    if [[ -z "$mt" ]]; then
      echo "LIVE bridge-artifact $mark (mtime unreadable -- fail-closed)"; hit=1; continue
    fi
    age=$((now - mt))
    if [[ "$age" -lt "$MINT_TEARDOWN_FRESH_S" ]]; then
      echo "LIVE bridge-artifact $mark (${age}s old)"; hit=1
    fi
  done

  [[ "$hit" -eq 1 ]] && return 0
  return 1
}

# _mt_flow_evidence <root> <instance_root> -- DGN-1683: prints FLOW or
# FLOW-STOPPED when MINT_TEARDOWN_FLOW_RECEIPT proves <root> is the unfinished
# target of the flow now aborting under <instance_root>; prints nothing
# otherwise (also when no receipt is supplied). Read-only.
_mt_flow_evidence() {
  local root="$1" instance_root="${2:-}"
  [[ -n "${MINT_TEARDOWN_FLOW_RECEIPT:-}" && -n "$instance_root" ]] || return 0
  pack_resolve_py3 || return 0
  "$PACK_PY3" "$_MT_LIB_DIR/mint_flow_guard.py" receipt-verify \
      --receipt "$MINT_TEARDOWN_FLOW_RECEIPT" --root "$root" \
      --instance-root "$instance_root" 2>/dev/null || true
}

# mint_teardown_evidence <root> [instance_root] -- prints TXN / FLOW /
# NEVER-RAN / NONE.
# The positive half of the gate: what makes this target an UNFINISHED MINT
# rather than merely a stopped agent.
mint_teardown_evidence() {
  local root="$1" instance_root="${2:-}" mark flow_ev
  if [[ -f "$root/$MINT_TEARDOWN_TXN_FILE" ]]; then echo "TXN"; return 0; fi
  flow_ev="$(_mt_flow_evidence "$root" "$instance_root")"
  if [[ -n "$flow_ev" ]]; then echo "FLOW"; return 0; fi
  [[ -f "$root/.instance.conf" ]] || { echo "NONE"; return 0; }
  for mark in "${MINT_TEARDOWN_RUNTIME_MARKS[@]}"; do
    [[ -e "$root/$mark" ]] && { echo "NONE"; return 0; }
  done
  echo "NEVER-RAN"
}

# mint_occupancy_probe <root> -- DGN-1543 R6: the ENTRY-side twin of the two
# teardown gates above. READ-ONLY. Prints one "OCCUPIED <kind> <detail>" line
# per signal. rc 0 = occupied (>=1 signal), rc 1 = free.
#
# THE PREDICATE (deliberately narrow, stated so the next reader can disagree
# with a fact instead of a feeling): occupied <=> the target root EXISTS and
# is NON-EMPTY. That set is (1) exactly the state a mint would overwrite,
# and (2) exactly the state the offered remedy -- mint_teardown_apply -- can
# archive. Signals that can exist WITHOUT the directory (a crew.conf ghost
# row, a stale minting-state row, an orphaned launchd registration) are
# deliberately NOT verdict drivers: teardown cannot clear any of them, so a
# refusal built on them would hand the owner a choice with no machine behind
# it. They have their own verbs and their own tickets (the crew ghost row's
# verb landed with DGN-1543 R8: teardown now deregisters the root it
# archives, and PRE-EXISTING ghosts are detected/removed by
# scripts/pack/crew_leave.sh scan/leave -- still not a mint-refusal input). An EMPTY directory is free on purpose: the mint
# preflight (mint_run.sh) tolerates one, and this probe must never be
# stricter than the write it guards.
#
# Lines after the first are EVIDENCE, not extra verdicts -- they decide
# WHICH remedy is honest to offer:
#   evidence TXN / NEVER-RAN  -> teardown's guard 2 will accept; the flow
#                                may offer teardown-then-remint.
#   evidence NONE             -> a completed agent (or foreign residue);
#                                guard 2 will REFUSE, so offering teardown
#                                would be an empty promise. Abort lane only.
#   live ...                  -> guard 1 will refuse until the units go
#                                through the owner-gated bootout path.
mint_occupancy_probe() {
  local root="$1"
  # A residue with no instance is clearable by the mint preflight.  Report it
  # before BotFather without turning it into the predecessor-blocking verdict.
  if [[ ! -e "$root" ]]; then
    if ! _mt_transcript_clean "$root"; then
      echo "CLEARABLE transcript-residue $root has no instance body; mint preflight will archive it"
    fi
    return 1
  fi
  if [[ ! -d "$root" ]]; then
    echo "OCCUPIED non-directory $root exists and is not a directory"
    return 0
  fi
  [[ -n "$(ls -A "$root" 2>/dev/null)" ]] || return 1
  echo "OCCUPIED instance-dir $root is not empty"
  [[ -f "$root/.instance.conf" ]] \
    && echo "OCCUPIED minted-instance .instance.conf present"
  echo "OCCUPIED evidence $(mint_teardown_evidence "$root")"
  if ! _mt_transcript_clean "$root"; then
    echo "OCCUPIED transcript-residue workspace read key is not empty"
  fi
  # Liveness detail rides the same probe teardown's guard 1 uses -- one
  # implementation, two readers. rc guard: liveness returns 1 when clean,
  # which must not kill a `set -e` caller.
  local live=""
  live="$(mint_teardown_liveness "$root" || true)"
  [[ -n "$live" ]] && printf '%s\n' "$live" | sed 's/^LIVE /OCCUPIED live /'
  return 0
}

# mint_teardown_txn_open <root> <slug> <opened_by> [parent_instance_root]
# Opens the transaction marker. Called by mint_run.sh the moment an instance
# tree exists; the caller removes it on success (mint_teardown_txn_close).
mint_teardown_txn_open() {
  local root="$1" slug="$2" by="$3" parent="${4:-}"
  [[ -d "$root" ]] || return 0
  pack_resolve_py3 || return 1
  "$PACK_PY3" - "$root/$MINT_TEARDOWN_TXN_FILE" "$slug" "$root" "$by" "$parent" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <<'PYEOF'
import json, sys
path, slug, root, by, parent, ts = sys.argv[1:7]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"slug": slug, "root": root, "opened_by": by,
               "parent_instance_root": parent, "opened_at": ts}, f, indent=2)
    f.write("\n")
PYEOF
}

mint_teardown_txn_close() { # mint_teardown_txn_close <root>
  rm -f "$1/$MINT_TEARDOWN_TXN_FILE"
}

# mint_teardown_plan <root> <archive_root> [instance_root] [handoff_root]
# The whole judgement, no writes. Prints PLAN lines on stdout and the refusal
# reason on stderr. rc: 0 proceed / 1 not a teardown target (usage-level) /
# 3 refused, instance looks live / 4 refused, no positive evidence /
# 5 refused, a crew transaction holds a lease on a kit this root is a
# registered member of (DGN-1543 R8).
# 7 refused, health observer state unreadable or its markers not confirmed
# by readback (DGN-1616; apply only for the readback half).
# Apply only: 6 crew row still present after leave / 8 a step did not land
# when OBSERVED afterwards (DGN-1615 axis 2 -- see _mt_verify_gone).
mint_teardown_plan() {
  local root="$1" archive_root="$2" instance_root="${3:-}" handoff_root="${4:-}"
  local phys inst_phys slug label ev live crew_rows crew_row crew_kit installed

  phys="$(_mt_phys "$root")"
  [[ -n "$phys" && -d "$phys" ]] || {
    echo "[teardown] REFUSE: not a directory: $root" >&2; return 1; }
  [[ "$phys" != "/" && "$phys" != "$HOME" ]] || {
    echo "[teardown] REFUSE: refusing to target \$HOME or /: $phys" >&2; return 1; }
  [[ -L "$root" ]] && {
    echo "[teardown] REFUSE: target is a symlink: $root (name the physical root)" >&2; return 1; }
  [[ -f "$phys/.instance.conf" ]] || {
    echo "[teardown] REFUSE: no .instance.conf at $phys -- not a minted instance, refusing to guess" >&2
    return 1; }
  if [[ -n "$instance_root" ]]; then
    inst_phys="$(_mt_phys "$instance_root")"
    [[ "$phys" != "$inst_phys" ]] || {
      echo "[teardown] REFUSE: target is the CALLING instance ($phys)" >&2; return 1; }
  fi

  local flow_ev waive=0
  flow_ev="$(_mt_flow_evidence "$phys" "$instance_root")"
  [[ "$flow_ev" == "FLOW-STOPPED" ]] && waive=1
  live="$(mint_teardown_liveness "$phys" "$waive" || true)"
  if [[ -n "$live" ]]; then
    echo "[teardown] REFUSE: instance looks alive -- teardown never touches a running agent:" >&2
    printf '%s\n' "$live" | sed 's/^/[teardown]   /' >&2
    echo "[teardown]   a registered unit is stopped only through the owner-gated path; this verb never boots out." >&2
    _mt_bootout_targets "$live" | sed 's/^/[teardown]   /' >&2
    grep -q '^LIVE launchd-unit ' <<< "$live" \
      && echo "[teardown]   bootout targets are plist Labels, not file names; this verb re-checks registration itself on the next run (a bootout's exit code is not trusted)." >&2
    return 3
  fi

  ev="$(mint_teardown_evidence "$phys" "$instance_root")"
  if [[ "$ev" == "NONE" ]]; then
    echo "[teardown] REFUSE: no unfinished-mint evidence at $phys" >&2
    echo "[teardown]   this instance carries bridge-runtime artifacts (it has run before) and no" >&2
    echo "[teardown]   $MINT_TEARDOWN_TXN_FILE marker -- that is a stopped agent, not a half-finished mint." >&2
    return 4
  fi

  # GUARD 3 (DGN-1543 R8) -- crew memberships this root holds. A held lease
  # on any of those kits means a live crew transaction owns that registry
  # right now (crew_lease.sh doctrine); archiving a member mid-transaction
  # would both invalidate the transaction's member set and leave a row this
  # apply could not remove (crew_leave refuses under a lease, update_seed M3
  # rule). Refuse the WHOLE teardown up front -- never archive first and
  # discover the registry is locked after the move.
  crew_rows="$(_crew_leave_matches "$phys")"
  while IFS= read -r crew_row; do
    [[ -n "$crew_row" ]] || continue
    crew_kit="${crew_row%% *}"
    if _crew_lease_active "$crew_kit"; then
      echo "[teardown] REFUSE: crew transaction in flight -- kit '$crew_kit' holds a lease (state=$(_crew_lease_state "$crew_kit")) and this instance is a registered crew member. Tearing it down now would mutate the crew registry under the transaction's feet. Retry after the transaction ends (or clears its lease)." >&2
      return 5
    fi
  done <<< "$crew_rows"

  slug="$(grep -E '^SLUG=' "$phys/config/agent.conf" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')" || slug=""
  [[ -n "$slug" ]] || slug="$(basename "$phys")"

  echo "PLAN root $phys"
  echo "PLAN evidence $ev"
  echo "PLAN slug $slug"
  echo "PLAN archive-root $archive_root"
  [[ -f "$phys/.telegram_bot/.env" ]] && echo "PLAN bot-token-carried .telegram_bot/.env"
  installed="$(_mt_launchagent_paths "$phys")" || {
    echo "[teardown] REFUSE: cannot determine installed plist ownership" >&2; return 1; }
  while IFS= read -r label; do
    [[ -n "$label" ]] && echo "PLAN launchagent-copy $label"
  done <<< "$installed"
  # DGN-1616: labels the health observer will otherwise report as silent
  # deaths. Unreadable observer state is a refusal, never "nothing to mark".
  local observer
  observer="$(_mt_observer_known "$phys")" || {
    echo "[teardown] REFUSE: cannot read the health observer state -- teardown would leave its units to alarm as silent deaths (DGN-1616)" >&2
    return 7; }
  printf '%s\n' "$observer" | sed -n 's/^RETIRE /PLAN observer-retire /p'
  printf '%s\n' "$observer" | sed -n 's/^ALERTED /PLAN observer-alerted /p'
  [[ -n "$handoff_root" && -d "$handoff_root/$slug" ]] \
    && echo "PLAN handoff-mailbox $handoff_root/$slug"
  [[ -n "$instance_root" && -f "$instance_root/.mint-journal/$slug.json" ]] \
    && echo "PLAN mint-journal $slug.json"
  # Transcript keys encode the caller's workspace spelling. Do not feed this
  # path through _mt_phys: /var -> /private/var would become a different key.
  if ! _mt_transcript_clean "$root"; then
    while IFS= read -r label; do
      [[ -n "$label" ]] && echo "PLAN transcript-family $label"
    done < <(_mt_transcript_list "$root")
  fi
  # DGN-1543 R8: every crew.conf row registering this root. Apply removes
  # each one AFTER the archive move (the root is then physically gone, which
  # is exactly crew_leave's proceed lane); restore re-adds them.
  while IFS= read -r crew_row; do
    [[ -n "$crew_row" ]] || continue
    echo "PLAN crew-member $crew_row"
  done <<< "$crew_rows"
  return 0
}

# mint_teardown_apply <root> <archive_root> [instance_root] [handoff_root]
# Runs mint_teardown_plan first and propagates its refusal verbatim, so the
# gate cannot be bypassed by calling apply directly. Prints the archive dir
# on the last stdout line.
mint_teardown_apply() {
  local root="$1" archive_root="$2" instance_root="${3:-}" handoff_root="${4:-}"
  local plan rc phys slug arch base n=0 label src
  local crew_row crew_kit crew_stored leave_out leave_bak leave_fail=0

  plan="$(mint_teardown_plan "$root" "$archive_root" "$instance_root" "$handoff_root")"; rc=$?
  [[ "$rc" -eq 0 ]] || return "$rc"

  phys="$(printf '%s\n' "$plan" | awk '$1=="PLAN"&&$2=="root"{print $3}')"
  slug="$(printf '%s\n' "$plan" | awk '$1=="PLAN"&&$2=="slug"{print $3}')"
  # Include residue that appeared after plan but before apply in the
  # pre-move receipt too; the archive itself repeats the same predicate below.
  if ! _mt_transcript_clean "$root" \
      && ! printf '%s\n' "$plan" | grep -q '^PLAN transcript-family '; then
    while IFS= read -r label; do
      [[ -n "$label" ]] && plan="$plan"$'\n'"PLAN transcript-family $label"
    done < <(_mt_transcript_list "$root")
  fi
  base="$archive_root/$(basename "$phys").$(_mt_ts)"
  arch="$base"
  while [[ -e "$arch" ]]; do n=$((n + 1)); arch="$base-$n"; done

  # DGN-1616: retire the observer's labels BEFORE the first move, confirmed
  # by readback. Not confirmed -> nothing moves (rc 7): a teardown that
  # finishes without markers is exactly the false "silent death" alarm.
  # Timing: guard 1a already proved every unit is booted out, and a marker
  # on a still-loaded label would be erased by the next poll -- so after
  # bootout, before the observer's grace window closes, is the right slot.
  local obs_labels obs_tmp=""
  obs_labels="$(printf '%s\n' "$plan" | awk '$1=="PLAN"&&$2=="observer-retire"{print $3}')"
  if [[ -n "$obs_labels" ]]; then
    obs_tmp="$(mktemp)"
    # shellcheck disable=SC2086  # labels carry no whitespace (plist_inventory)
    if ! _mt_observer_mark "$phys" "$obs_tmp" "teardown-$(basename "$arch")" \
        "intentional teardown of unfinished mint $slug (mint_run.sh teardown, DGN-1616)" \
        $obs_labels; then
      rm -f "$obs_tmp"
      echo "[teardown] REFUSE: health observer markers not confirmed by readback -- nothing was moved (DGN-1616)" >&2
      return 7
    fi
    while IFS= read -r label; do
      echo "[teardown] observer marked retired -> $label"
    done <<< "$obs_labels"
  fi
  while IFS= read -r label; do
    [[ -n "$label" ]] || continue
    echo "[teardown] NOTE: the health observer already alerted on ${label%% *} at ${label#* } -- that alert is about a unit of this instance, which this teardown is removing on purpose"
  done < <(printf '%s\n' "$plan" | sed -n 's/^PLAN observer-alerted //p')

  mkdir -p "$arch" || return 1
  chmod 700 "$arch" 2>/dev/null || true
  [[ -n "$obs_tmp" ]] && mv "$obs_tmp" "$arch/observer.json"

  # Receipt BEFORE the first move: a crash mid-teardown must still leave a
  # readable record of what was being moved where (same doctrine as the mint
  # journal's pre-record).
  _mt_receipt "$arch" "$phys" "$slug" "in-progress" "$plan" || return 1

  if ! _mt_transcript_clean "$root"; then
    label="$(_mt_transcript_archive "$root" "$arch/transcripts")" || return 1
    [[ -n "$label" ]] || return 1
    echo "[teardown] archived transcript family -> $arch/transcripts"
  fi
  # Labels are read now: after the move the plists live under the archive.
  local own_labels verify_fail=0
  own_labels="$(mint_teardown_labels "$phys")" || own_labels=""
  mv "$phys" "$arch/instance" || return 1
  _mt_verify_gone "instance body" "$phys" "$arch/instance/.instance.conf" || return 8
  echo "[teardown] archived instance -> $arch/instance"

  while IFS= read -r label; do
    [[ -n "$label" ]] || continue
    src="$label"
    [[ -f "$src" ]] || continue
    mkdir -p "$arch/LaunchAgents"
    mv "$src" "$arch/LaunchAgents/"
    if _mt_verify_gone "launchagent copy" "$src" "$arch/LaunchAgents/$(basename "$src")"; then
      echo "[teardown] archived launchagent copy -> $(basename "$src")"
    else
      verify_fail=1
    fi
  done < <(printf '%s\n' "$plan" | awk '$1=="PLAN"&&$2=="launchagent-copy"{print $3}')

  if [[ -n "$handoff_root" && -d "$handoff_root/$slug" ]]; then
    mkdir -p "$arch/handoff"
    mv "$handoff_root/$slug" "$arch/handoff/"
    if _mt_verify_gone "handoff mailbox" "$handoff_root/$slug" "$arch/handoff/$slug"; then
      echo "[teardown] archived handoff mailbox -> $slug"
    else
      verify_fail=1
    fi
  fi
  if [[ -n "$instance_root" && -f "$instance_root/.mint-journal/$slug.json" ]]; then
    mkdir -p "$arch/mint-journal"
    mv "$instance_root/.mint-journal/$slug.json" "$arch/mint-journal/"
    if _mt_verify_gone "mint journal" "$instance_root/.mint-journal/$slug.json" "$arch/mint-journal/$slug.json"; then
      echo "[teardown] archived mint journal -> $slug.json"
    else
      verify_fail=1
    fi
  fi

  # DGN-1543 R8: deregister every crew membership the plan recorded. The
  # instance body just moved, so the stored root is now a dead path -- the
  # exact GHOST shape crew_leave's predicate accepts. crew.conf is backed up
  # (crew.conf.bak-leave-teardown-<slug>-<ts>) before each removal, and the
  # backup path lands in the receipt. A failure here is LOUD and fails the
  # verb (rc 6): an archived instance still registered is precisely the
  # ghost-member defect this wiring exists to close -- but only after the
  # receipt records everything that did move.
  while IFS= read -r crew_row; do
    [[ -n "$crew_row" ]] || continue
    crew_kit="${crew_row%% *}"; crew_stored="${crew_row#* }"
    if leave_out="$(_crew_leave_one "$crew_kit" "$crew_stored" "teardown-$slug" 0)"; then
      printf '%s\n' "$leave_out" | grep -v '^LEFT ' | sed 's/^/[teardown] /'
      leave_bak="$(printf '%s\n' "$leave_out" | awk '$1=="LEFT"{print $3}')"
      [[ -n "$leave_bak" ]] \
        && plan="$plan"$'\n'"PLAN crew-left $crew_kit $leave_bak" \
        && echo "[teardown] crew deregistered -> $crew_kit (backup $leave_bak)"
    else
      echo "[teardown] ERROR: crew leave failed for kit '$crew_kit' -- the archived instance may remain a GHOST member in its crew.conf. Remove it with: scripts/pack/crew_leave.sh leave --root $crew_stored --kit $crew_kit" >&2
      leave_fail=1
    fi
  done < <(printf '%s\n' "$plan" | sed -n 's/^PLAN crew-member //p')

  # DGN-1615 axis 2: re-observe instead of trusting the verbs above.
  # (a) crew.conf: the leave verb's rc is not the proof -- re-run the same
  #     membership predicate; any surviving row is a ghost.
  while IFS= read -r crew_row; do
    [[ -n "$crew_row" ]] || continue
    echo "[teardown] ERROR: crew row still registered after leave: $crew_row -- remove it with: scripts/pack/crew_leave.sh leave --root ${crew_row#* } --kit ${crew_row%% *}" >&2
    leave_fail=1
  done < <(_crew_leave_matches "$phys")
  # (b) launchd: guard 1a proved every unit unregistered before the move; a
  #     unit registered NOW was loaded again behind this verb's back.
  local uid; uid="$(id -u)"
  while IFS= read -r label; do
    [[ -n "$label" ]] || continue
    if _mt_launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
      echo "[teardown] ERROR: launchd unit registered again after the archive: $label (its plist now lives under $arch)" >&2
      verify_fail=1
    fi
  done <<< "$own_labels"

  local status=complete
  [[ "$leave_fail" -eq 0 && "$verify_fail" -eq 0 ]] || status=incomplete
  _mt_receipt "$arch" "$phys" "$slug" "$status" "$plan" || return 1
  printf '%s\n' "$arch"
  [[ "$verify_fail" -eq 0 ]] || return 8
  [[ "$leave_fail" -eq 0 ]] || return 6
}

# _mt_receipt <archive> <root> <slug> <status> <plan>
_mt_receipt() {
  local arch="$1" root="$2" slug="$3" status="$4" plan="$5" tmp rc
  pack_resolve_py3 || return 1
  # WHY THE PLAN GOES THROUGH A TEMP FILE AND NOT STDIN: `python3 - <<PY`
  # already spends stdin on the SCRIPT SOURCE, so a second redirection for
  # the data silently wins the fd and the interpreter reads the data as
  # code. skill_retire_diff hit exactly this and left the same note.
  tmp="$(mktemp)"
  printf '%s\n' "$plan" > "$tmp"
  "$PACK_PY3" - "$arch/RECEIPT.json" "$root" "$slug" "$status" "$HOME" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tmp" <<'PYEOF'
import json, sys
receipt, root, slug, status, home, ts, plan_file = sys.argv[1:8]
with open(plan_file, encoding="utf-8") as f:
    plan = f.read().splitlines()
fields = {}
for line in plan:
    parts = line.split(None, 2)
    if len(parts) == 3 and parts[0] == "PLAN":
        fields.setdefault(parts[1], []).append(parts[2])
data = {
    "ticket": "DGN-1506",
    "status": status,
    "torn_down_at": ts,
    "slug": slug,
    "original_root": root,
    "evidence": (fields.get("evidence") or [""])[0],
    # The token is NOT destroyed: it rode into the archive inside the
    # instance .env. Revoking it needs the user's own BotFather auth, which
    # no script may drive -- so the fact is recorded, not acted on.
    "bot_token_present": bool(fields.get("bot-token-carried")),
    "bot_token_archived_at": ("instance/.telegram_bot/.env"
                              if fields.get("bot-token-carried") else None),
    "moved": {
        "instance": "instance",
        "launchagent_copies": fields.get("launchagent-copy", []),
        "handoff_mailbox": fields.get("handoff-mailbox", []),
        "mint_journal": fields.get("mint-journal", []),
        "transcript_family": fields.get("transcript-family", []),
    },
    # DGN-1543 R8: crew rows this root held ("<kit> <stored_root>") and the
    # removals actually performed ("<kit> <crew.conf backup path>"). The
    # crew.conf row is the ONLY thing not merely moved: it is removed with
    # an on-disk backup, and restore re-adds it from crew_membership.
    "crew_membership": fields.get("crew-member", []),
    "crew_left": fields.get("crew-left", []),
    # DGN-1616: health-observer labels marked retired before the first move
    # (detail in observer.json), and those whose loss alarm had already
    # fired. Restore leaves the markers: the restored units are still down,
    # and the observer's own poll drops a marker once its unit loads again.
    "observer_retired": fields.get("observer-retire", []),
    "observer_alerted_before_teardown": fields.get("observer-alerted", []),
    "home_at_teardown": home,
    "restore": "mint_run.sh teardown --restore <this directory>",
}
with open(receipt, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
  rc=$?
  rm -f "$tmp"
  return $rc
}

# mint_teardown_restore <archive-dir> [handoff_root] [instance_root]
# The way back from the way back. Moves every recorded piece home, and
# refuses (touching nothing) if ANY destination is already occupied -- a
# half-restore is worse than no restore. Health-observer retired markers
# (DGN-1616) are deliberately left in place: the restored units are still
# down, and the observer's own poll drops a marker once its unit loads again.
mint_teardown_restore() {
  local arch="$1" handoff_root="${2:-}" instance_root="${3:-}"
  local receipt root slug f dst crew_rows crew_row crew_kit crew_stored
  receipt="$arch/RECEIPT.json"
  [[ -f "$receipt" ]] || { echo "[teardown] REFUSE: no RECEIPT.json in $arch" >&2; return 1; }
  pack_resolve_py3 || return 1
  root="$("$PACK_PY3" -c 'import json,sys; print(json.load(open(sys.argv[1]))["original_root"])' "$receipt")"
  slug="$("$PACK_PY3" -c 'import json,sys; print(json.load(open(sys.argv[1]))["slug"])' "$receipt")"
  [[ -n "$root" ]] || { echo "[teardown] REFUSE: receipt has no original_root" >&2; return 1; }
  [[ -e "$root" ]] && { echo "[teardown] REFUSE: original root is occupied again: $root" >&2; return 1; }
  [[ -d "$arch/instance" ]] || { echo "[teardown] REFUSE: archive holds no instance/ body" >&2; return 1; }

  # DGN-1543 R8: crew rows to re-register ("<kit> <stored_root>", pre-R8
  # receipts simply have none). Checked BEFORE any move: re-adding a member
  # row while that kit's crew transaction holds a lease is the same
  # registry-under-the-feet hazard the plan-side guard refuses (fail-closed,
  # and a half-restore is worse than no restore).
  crew_rows="$("$PACK_PY3" -c 'import json,sys
for e in json.load(open(sys.argv[1])).get("crew_membership", []):
    print(e)' "$receipt")"
  while IFS= read -r crew_row; do
    [[ -n "$crew_row" ]] || continue
    crew_kit="${crew_row%% *}"
    if _crew_lease_active "$crew_kit"; then
      echo "[teardown] REFUSE: crew transaction in flight -- kit '$crew_kit' holds a lease (state=$(_crew_lease_state "$crew_kit")) and this restore would re-register a member row in its registry. Retry after the transaction ends." >&2
      return 1
    fi
  done <<< "$crew_rows"

  local launchagents_dir
  launchagents_dir="$(_mt_launchagents_dir)"
  for f in "$arch"/LaunchAgents/*.plist; do
    [[ -e "$f" ]] || continue
    dst="$launchagents_dir/$(basename "$f")"
    [[ -e "$dst" ]] && { echo "[teardown] REFUSE: occupied: $dst" >&2; return 1; }
  done
  if [[ -n "$handoff_root" && -d "$arch/handoff/$slug" && -e "$handoff_root/$slug" ]]; then
    echo "[teardown] REFUSE: occupied: $handoff_root/$slug" >&2; return 1
  fi
  if [[ -d "$arch/transcripts" ]]; then
    _mt_transcript_restore_check "$arch/transcripts" || {
      echo "[teardown] REFUSE: transcript restore destination is occupied" >&2
      return 1
    }
  fi

  mkdir -p "$(dirname "$root")"
  mv "$arch/instance" "$root" || return 1
  echo "[teardown] restored instance -> $root"
  if [[ -d "$arch/transcripts" ]]; then
    _mt_transcript_restore "$arch/transcripts" >/dev/null || return 1
    echo "[teardown] restored transcript family"
  fi
  mkdir -p "$launchagents_dir" 2>/dev/null || return 1
  for f in "$arch"/LaunchAgents/*.plist; do
    [[ -e "$f" ]] || continue
    mv "$f" "$launchagents_dir/" && echo "[teardown] restored launchagent copy -> $(basename "$f")"
  done
  if [[ -n "$handoff_root" && -d "$arch/handoff/$slug" ]]; then
    mkdir -p "$handoff_root"
    mv "$arch/handoff/$slug" "$handoff_root/" && echo "[teardown] restored handoff mailbox -> $slug"
  fi
  if [[ -n "$instance_root" && -f "$arch/mint-journal/$slug.json" ]]; then
    mkdir -p "$instance_root/.mint-journal"
    mv "$arch/mint-journal/$slug.json" "$instance_root/.mint-journal/" \
      && echo "[teardown] restored mint journal -> $slug.json"
  fi
  # DGN-1543 R8: re-register the crew rows the teardown removed (the exact
  # stored line, append-if-absent -- idempotent like the seed it mirrors).
  # The instance body is already back, so the 3-source membership picture is
  # whole again the moment the row lands.
  while IFS= read -r crew_row; do
    [[ -n "$crew_row" ]] || continue
    crew_kit="${crew_row%% *}"; crew_stored="${crew_row#* }"
    _crew_rejoin_upsert "$crew_kit" "$crew_stored" \
      && echo "[teardown] restored crew membership -> $crew_kit"
  done <<< "$crew_rows"
  # The restored tree is an unfinished mint again by definition.
  echo "[teardown] DONE -- restored from $arch"
}
