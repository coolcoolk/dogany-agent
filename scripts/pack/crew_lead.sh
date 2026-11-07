#!/bin/bash
# crew_lead.sh -- DGN-2040: a secretary-style leader of several crews.
# Design: the DGN-2040 secretary-leader design, rev2, sections 3 and 5.5
# (canonical repo; not shipped).
#
# A crew leader (DOGANY_CREW_ROLE=leader) holds no kit in its own root. For
# each crew it leads it owns one SEAT: a one-kit member root at
# $SHARED_HOME/seats/<kit>/<leader-name>, joined to that crew like any member
# (DB link, migrate, crew.conf row, crew updates) with no schedules, routine
# bundle, hooks or activated skills (pack_install.sh KIT_SEAT=1). The leader
# writes a crew's ledger only by running that kit's own command in that seat
# (`run`). Both keys are instance properties written only here, never
# derived from an agent pack or kit id (dec-400).
#
# Verbs:
#   role   --leader <L> [--clear]
#          Set (or clear) DOGANY_CREW_ROLE=leader in L's conf. Set refuses if
#          L holds any kit or any database/*.db; clear refuses while L owns
#          a seat.
#   seat   --kit <K> --leader <L> [--owner-yes] [--dry-run]
#          [--pack-dir <dir>] [--catalog <file>] [--source-tag <tag>]
#          [--untagged-local <reason>]
#          Mint L's seat of crew K and join it through pack_install.sh.
#          Refuses unless L is a leader, crew K exists with a live non-seat
#          member (C7), no other leader holds a seat of K (C6), L has no seat
#          of K yet, crew.declared is absent (G7) and no .lease is held. The
#          the selected crew is the join request. The old --owner-yes flag
#          remains accepted. Writes the crew-<K> verb index skill
#          on L, then `check --leader L` must pass or the seat is rolled back.
#   unseat --kit <K> --leader <L> [--dry-run]
#          Archive the seat (receipts kept) to seats/_archive/, then
#          crew_leave.sh drops its member= row; removes the index skill.
#   run    --kit <K> [--leader <L>] [--fresh] -- <cmd> [args...]
#          The leader's only write path (section 3.3). <cmd> must be a file
#          under the seat's database/ or .claude/skills-bundle/, optionally
#          behind python3/bash; sqlite3, `python3 -c`, flags before the script
#          and anything outside the seat are refused (N11). Refuses with 76 on
#          a dirty crew lease. Exports PROJECT_ROOT=<seat>, DOGANY_ACTOR=<leader
#          name>, DOGANY_INBOUND_CONTEXT=<L>/.telegram_bot/inbound-context.json.
#          Receipts: <seat>/runtime/lead-receipts.jsonl. A repeat of the same
#          argv in the same owner turn (request_id) with a recorded rc=0
#          replays the recorded stdout instead of re-executing; --fresh forces
#          a real run; no request_id never replays. L defaults to
#          $PROJECT_ROOT, else the cwd.
#   check  [--leader <L>]
#          Read-only. RED lines for the section 5.5 shapes (rc 1), WARN lines
#          for kits that have not adopted DOGANY_ACTOR / DOGANY_INBOUND_CONTEXT
#          and for a kit_dev ACTORS.md role that is not `leader` (G9, never
#          red). rc 0 = no red.
#
# Exit codes: 0 ok, 1 refused / red, 2 usage, 76 crew update in progress
# (run), otherwise the kit command's own rc (run).
#
# Env: DOGANY_SHARED_HOME / DOGANY_CREW_DIR (layout override, same
# resolution as crew_leave.sh / update_seed.sh).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

PY_RESOLVE_LIB="$SCRIPT_DIR/lib/py_resolve.sh"
[[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
  || { echo "ERROR: python resolver library missing ($PY_RESOLVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/py_resolve.sh
. "$PY_RESOLVE_LIB"
pack_resolve_py3 || exit 1

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
SEATS_DIR="$SHARED_HOME/seats"
CL_PY="$SCRIPT_DIR/lib/crew_lead.py"

# shellcheck source=lib/crew_seat.sh
. "$SCRIPT_DIR/lib/crew_seat.sh"
# shellcheck source=lib/crew_lease.sh
. "$SCRIPT_DIR/lib/crew_lease.sh"

_say()    { echo "[crew-lead] $*"; }
_refuse() { echo "[crew-lead] REFUSED: $*" >&2; exit 1; }

usage() {
  sed -n '/^# Verbs:/,/^# Exit codes:/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# _members <kit> -- the crew.conf member roots, as written.
_members() {
  sed -n 's/^member=//p' "$CREW_DIR/$1/crew.conf" 2>/dev/null
}

# _is_member <kit> <root> -- rc 0 iff <root> is a member= row of <kit>
# (as written, or the same physical directory).
_is_member() {
  local kit="$1" root="$2" phys m
  phys="$(_seat_phys "$root")"
  while IFS= read -r m; do
    [[ -n "$m" ]] || continue
    [[ "$m" == "$root" ]] && return 0
    [[ -n "$phys" && "$(_seat_phys "$m")" == "$phys" ]] && return 0
  done < <(_members "$kit")
  return 1
}

# _packs_kits <conf> -- the kit ids recorded in DOGANY_PACKS (catalog
# kind=kit rows; an id with no catalog row counts when it also has a
# database/<id>.db, so an unknown kit is not missed).
_packs_kits() {
  "$PACK_PY3" "$CL_PY" packs-kits "$1" "$REPO_DIR/packs/catalog.json" 2>/dev/null || true
}

# _conf_set <conf> <KEY> [value] -- drop every KEY= line, append KEY=value
# when a value is given. Atomic (temp file + rename in the same dir).
_conf_set() {
  local conf="$1" key="$2" val="${3-}" tmp
  tmp="$(mktemp "$conf.crew-lead.XXXXXX")" || return 1
  grep -v "^$key=" "$conf" > "$tmp" 2>/dev/null || true
  [[ -n "$val" ]] && printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv -f "$tmp" "$conf"
}

_valid_kit() {
  [[ "$1" =~ ^[a-z][a-z0-9_-]{0,31}$ && "$1" != "_archive" ]]
}

# _leader_phys <L> -- physical leader root or refuse.
_leader_phys() {
  local p
  [[ -n "$1" ]] || usage
  p="$(_seat_phys "$1")"
  [[ -n "$p" && -f "$p/.instance.conf" ]] || _refuse "leader root '$1' is not an instance (no .instance.conf)"
  printf '%s' "$p"
}

# ---------------------------------------------------------------------------
# role
# ---------------------------------------------------------------------------
cmd_role() {
  local L="" clear=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --leader) L="${2:-}"; shift 2 || usage ;;
      --clear)  clear=1; shift ;;
      *) usage ;;
    esac
  done
  L="$(_leader_phys "$L")"
  if [[ "$clear" -eq 1 ]]; then
    local seats
    seats="$(_seats_of_leader "$L")"
    [[ -z "$seats" ]] || _refuse "'$L' still owns seat(s): $(printf '%s' "$seats" | awk '{print $1}' | paste -sd, -) -- unseat them first (crew_lead.sh unseat --kit <K> --leader $L)"
    _conf_set "$L/.instance.conf" DOGANY_CREW_ROLE || _refuse "could not rewrite $L/.instance.conf"
    _say "role cleared on $L"
    return 0
  fi
  local kits dbs
  kits="$(_packs_kits "$L/.instance.conf" | paste -sd, -)"
  [[ -z "$kits" ]] || _refuse "'$L' holds kit(s) $kits in its own root -- a leader holds no kit (one kit per root; block (1) would strand it). Making a holder leader a secretary leader is an owner ruling (design section 12, O1)"
  dbs="$(cd "$L/database" 2>/dev/null && ls -1 -- *.db 2>/dev/null | paste -sd, -)"
  [[ -z "$dbs" ]] || _refuse "'$L' has database/*.db ($dbs) -- a leader root holds no kit DB"
  _conf_set "$L/.instance.conf" DOGANY_CREW_ROLE leader || _refuse "could not rewrite $L/.instance.conf"
  _say "role set: $L is a crew leader (DOGANY_CREW_ROLE=leader)"
}

# ---------------------------------------------------------------------------
# seat
# ---------------------------------------------------------------------------
# _seat_refusals <kit> <L> -- the section 5.5 seat step 1 refusals. Shared
# by the real and the dry run (same verdicts).
_seat_refusals() {
  local K="$1" L="$2" seat m owner live=0
  [[ "$(_crew_role "$L")" == "leader" ]] \
    || _refuse "'$L' is not a crew leader (DOGANY_CREW_ROLE=leader) -- run crew_lead.sh role --leader $L first"
  [[ -f "$CREW_DIR/$K/crew.conf" ]] \
    || _refuse "crew '$K' does not exist (no $CREW_DIR/$K/crew.conf) -- a seat joins an existing crew with staff (C7)"
  # G7: kept as designed while DGN-2056 fixes the declared-crew path.
  [[ ! -e "$CREW_DIR/$K/crew.declared" ]] \
    || _refuse "crew '$K' is a declared crew ($CREW_DIR/$K/crew.declared) -- no member can mount its kit yet (G7), so a seat cannot join it"
  [[ ! -e "$CREW_DIR/$K/.lease" ]] \
    || _refuse "crew '$K' has a lease held ($CREW_DIR/$K/.lease) -- a crew transaction is in progress or was never cleared"
  seat="$(_seat_path "$K" "$L")"
  [[ ! -e "$seat" ]] || _refuse "'$L' already has a seat of '$K' ($seat) -- one seat per crew (C6)"
  _is_member "$K" "$seat" && _refuse "'$seat' is already a member= of crew '$K' -- one seat per crew (C6)"
  while IFS= read -r m; do
    [[ -n "$m" ]] || continue
    owner="$(_seat_of "$m")"
    if [[ -n "$owner" ]]; then
      [[ "$(_seat_phys "$owner")" == "$L" ]] \
        || _refuse "crew '$K' already has a seat of another leader ($m, owner $owner) -- each crew has at most one secretary leader (C6)"
      continue
    fi
    if [[ -d "$m" ]] && grep -Eq "^DOGANY_PACKS=(.*,)?$K@" "$m/.instance.conf" 2>/dev/null; then
      live=$((live + 1))
    fi
  done < <(_members "$K")
  [[ "$live" -ge 1 ]] \
    || _refuse "crew '$K' has no live non-seat member -- a secretary is not the department; the crew keeps its staff (C7)"
}

# _seat_rollback <kit> <seat> <why> -- archive the seat body, then drop its
# member= row (crew_leave refuses a FULL member, so the body goes first:
# the teardown order).
_seat_rollback() {
  local K="$1" seat="$2" why="$3" arch
  arch="$SEATS_DIR/_archive/$K/$(basename "$seat")-$why-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$(dirname "$arch")" && mv "$seat" "$arch" \
    || { echo "[crew-lead] ERROR: could not archive $seat -- remove it by hand after review" >&2; return 1; }
  _say "seat body archived: $arch"
  if _is_member "$K" "$seat"; then
    DOGANY_SHARED_HOME="$SHARED_HOME" DOGANY_CREW_DIR="$CREW_DIR" \
      bash "$SCRIPT_DIR/crew_leave.sh" leave --root "$seat" --kit "$K" --tag "unseat-$(basename "$seat")" \
      || { echo "[crew-lead] ERROR: crew_leave.sh could not drop member=$seat from crew '$K' -- crew_leave.sh scan names it" >&2; return 1; }
  fi
  return 0
}

# _index_skill <kit> <L> <seat> -- the leader's crew-<kit> verb index
# (framework-owned, regenerated on every seat). Lists what the runner may
# execute in the seat; the kit's own handover_brief_verb output, when the
# kit declares one, is the description (read-only by contract).
_index_skill() {
  local K="$1" L="$2" seat="$3" dir brief="" verb
  dir="$L/.claude/skills/crew-$K"
  mkdir -p "$dir" || return 1
  verb="$("$PACK_PY3" "$CL_PY" manifest-field "$seat/config/packs/$K.manifest.json" handover_brief_verb 2>/dev/null)"
  if [[ -n "$verb" && -x "$seat/database/$K.sh" ]]; then
    brief="$(cd "$seat" && PROJECT_ROOT="$seat" timeout 20 "$seat/database/$K.sh" "$verb" 2>/dev/null | head -c 4096)" || brief=""
  fi
  {
    printf -- '---\nname: crew-%s\ndescription: Read and write the %s crew ledger as its leader, through the kit'"'"'s own verbs in your seat.\n---\n\n' "$K" "$K"
    printf '# crew-%s (generated by crew_lead.sh -- DGN-2040; do not edit)\n\n' "$K"
    printf 'You lead the `%s` crew through your seat `%s`.\n' "$K" "$seat"
    printf 'Every read or write of this crew goes through:\n\n'
    printf '    bash %s/scripts/pack/crew_lead.sh run --kit %s -- <command> [args...]\n\n' "$REPO_DIR" "$K"
    printf '`<command>` is a path inside the seat. No sqlite3, no raw SQL, no direct edit of crew files.\n'
    printf 'Long domain sessions and kit setup flows belong to the crew staff.\n\n'
    [[ -n "$brief" ]] && printf '## What this crew keeps\n\n%s\n\n' "$brief"
    printf '## Commands\n\n'
    [[ -e "$seat/database/$K.sh" ]] && printf -- '- `database/%s.sh <verb> ...` (the kit CLI)\n' "$K"
    local f
    while IFS= read -r f; do
      printf -- '- `%s`\n' "${f#"$seat/"}"
    done < <(find "$seat/.claude/skills-bundle" -type f \( -name '*.py' -o -name '*.sh' \) 2>/dev/null | LC_ALL=C sort)
  } > "$dir/SKILL.md.tmp" && mv -f "$dir/SKILL.md.tmp" "$dir/SKILL.md"
}

cmd_seat() {
  local K="" L="" yes=0 dry=0 pi_args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --kit)       K="${2:-}"; shift 2 || usage ;;
      --leader)    L="${2:-}"; shift 2 || usage ;;
      --owner-yes) yes=1; shift ;;
      --dry-run|--dry) dry=1; shift ;;
      --pack-dir|--catalog|--source-tag|--untagged-local)
        [[ $# -ge 2 ]] || usage; pi_args+=("$1" "$2"); shift 2 ;;
      *) usage ;;
    esac
  done
  _valid_kit "$K" || usage
  L="$(_leader_phys "$L")"
  _seat_refusals "$K" "$L"
  local name seat
  name="$(basename "$L")"
  seat="$(_seat_path "$K" "$L")"
  # Selecting this crew is the join request; no separate owner sentinel.
  yes=1
  # 5.5 step 3: the seat installs the kit at the crew's version. With no
  # explicit source, pin the crew's tag; an explicit --pack-dir/--source-tag
  # is judged by the installer's own join-half gate (DGN-1682) instead.
  local has_src=0 a
  for a in ${pi_args[@]+"${pi_args[@]}"}; do
    case "$a" in --pack-dir|--source-tag|--untagged-local) has_src=1 ;; esac
  done
  if [[ "$has_src" -eq 0 ]]; then
    # shellcheck source=lib/crew_kit_version.sh
    . "$SCRIPT_DIR/lib/crew_kit_version.sh"
    _crew_kit_version "$K" "$seat" \
      || _refuse "cannot read which '$K' version the crew runs (estate scan failed) -- fail-closed"
    case "$CREW_KIT_STATE" in
      uniform)
        # shellcheck source=lib/pack_commit_gate.sh
        . "$SCRIPT_DIR/lib/pack_commit_gate.sh" || _refuse "pack_commit_gate.sh missing -- fail-closed"
        pi_args+=(--source-tag "$(pack_d1_tag_ref "$K" "$CREW_KIT_V")")
        _say "crew '$K' runs $CREW_KIT_V ($CREW_KIT_PEERS) -- the seat installs that version" ;;
      none) _refuse "no discovered instance mounts crew '$K' -- cannot read the crew's version; pass --source-tag explicitly" ;;
      *) _refuse "crew '$K' version is $CREW_KIT_STATE ($CREW_KIT_PEERS) -- bring the crew to one recorded version first" ;;
    esac
  fi
  if [[ "$dry" -eq 1 ]]; then
    _say "dry-run: would mint seat $seat (DOGANY_SEAT_OF=$L, class domain) and join the selected crew '$K' via pack_install.sh"
    _say "dry-run: would write $L/.claude/skills/crew-$K/SKILL.md; nothing written"
    return 0
  fi
  mkdir -p "$seat/config" "$seat/runtime" "$seat/database" "$seat/.claude/skills-bundle" \
    || _refuse "could not create $seat"
  {
    printf 'DOGANY_AGENT_NAME=%s\n' "$name"
    printf 'DOGANY_AGENT_CLASS=domain\n'
    printf 'DOGANY_SEAT_OF=%s\n' "$L"
    local lang
    lang="$(_seat_conf_last "$L/.instance.conf" DOGANY_LANG)"
    [[ -n "$lang" ]] && printf 'DOGANY_LANG=%s\n' "$lang"
  } > "$seat/.instance.conf"
  _say "seat minted: $seat (not registered in ~/.dogany/instances; seats are never discovered)"
  local rc=0
  DOGANY_SHARED_HOME="$SHARED_HOME" DOGANY_CREW_DIR="$CREW_DIR" \
  DOGANY_CREW_JOIN_ACK="$([[ "$yes" -eq 1 ]] && echo 1 || echo 0)" \
    bash "$SCRIPT_DIR/pack_install.sh" "$name" "$seat" --pack "$K" --no-start --no-state \
    ${pi_args[@]+"${pi_args[@]}"} || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    echo "[crew-lead] seat install of '$K' failed (rc=$rc) -- rolling the seat back" >&2
    _seat_rollback "$K" "$seat" "failed" || true
    exit 1
  fi
  _index_skill "$K" "$L" "$seat" || echo "[crew-lead] WARN: could not write the crew-$K index skill on $L" >&2
  if ! cmd_check --leader "$L"; then
    echo "[crew-lead] check --leader $L is red after seating '$K' -- rolling the seat back" >&2
    _seat_rollback "$K" "$seat" "check-red" || true
    rm -rf "$L/.claude/skills/crew-$K"
    exit 1
  fi
  _say "seated: $name leads crew '$K' through $seat"
}

# ---------------------------------------------------------------------------
# unseat
# ---------------------------------------------------------------------------
cmd_unseat() {
  local K="" L="" dry=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --kit)    K="${2:-}"; shift 2 || usage ;;
      --leader) L="${2:-}"; shift 2 || usage ;;
      --dry-run|--dry) dry=1; shift ;;
      *) usage ;;
    esac
  done
  _valid_kit "$K" || usage
  L="$(_leader_phys "$L")"
  local seat owner
  seat="$(_seat_path "$K" "$L")"
  [[ -d "$seat" ]] || _refuse "'$L' has no seat of '$K' ($seat)"
  owner="$(_seat_of "$seat")"
  [[ -n "$owner" && "$(_seat_phys "$owner")" == "$L" ]] \
    || _refuse "'$seat' is not a seat of '$L' (DOGANY_SEAT_OF='$owner')"
  # crew_leave's own lease rule, checked before the body moves.
  ! _crew_lease_active "$K" \
    || _refuse "crew '$K' has a lease held ($CREW_DIR/$K/.lease) -- unseat after the crew transaction clears"
  if [[ "$dry" -eq 1 ]]; then
    _say "dry-run: would archive $seat to $SEATS_DIR/_archive/$K/, drop member=$seat from crew '$K', remove $L/.claude/skills/crew-$K; nothing written"
    return 0
  fi
  _seat_rollback "$K" "$seat" "unseat" || exit 1
  rm -rf "$L/.claude/skills/crew-$K"
  _say "unseated: $(basename "$L") no longer leads crew '$K' (receipts kept in the archive)"
}

# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
cmd_run() {
  local K="" L="${PROJECT_ROOT:-$PWD}" fresh=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --kit)    K="${2:-}"; shift 2 || usage ;;
      --leader) L="${2:-}"; shift 2 || usage ;;
      --fresh)  fresh=1; shift ;;
      --) shift; break ;;
      *) usage ;;
    esac
  done
  _valid_kit "$K" || usage
  [[ $# -ge 1 ]] || usage
  L="$(_leader_phys "$L")"
  [[ "$(_crew_role "$L")" == "leader" ]] || _refuse "'$L' is not a crew leader -- only a leader writes through a seat"
  # 1. The seat: at seats/K/<basename L>, owned by L, a member of crew K.
  local seat owner
  seat="$(_seat_path "$K" "$L")"
  [[ -f "$seat/.instance.conf" ]] || _refuse "'$L' has no seat of crew '$K' ($seat)"
  owner="$(_seat_of "$seat")"
  [[ -n "$owner" && "$(_seat_phys "$owner")" == "$L" ]] \
    || _refuse "'$seat' is not a seat of '$L' (DOGANY_SEAT_OF='$owner')"
  _is_member "$K" "$seat" || _refuse "'$seat' is not a member= of crew '$K'"
  # 2. The command resolves inside the seat (N11).
  local resolved
  resolved="$("$PACK_PY3" "$CL_PY" resolve "$seat" "$SHARED_HOME/crews/$K/shared-skills" -- "$@")" \
    || _refuse "command resolution crashed -- fail-closed"
  case "$resolved" in
    OK\ *) ;;
    REFUSE\ *) _refuse "${resolved#REFUSE }" ;;
    *) _refuse "command resolution gave no verdict -- fail-closed" ;;
  esac
  local script_i="${resolved#OK }"; script_i="${script_i%% *}"
  local script_path="${resolved#OK * }"
  local argv=("$@")
  argv[$script_i]="$script_path"
  # 3. Lease belt (the section 6 hold is the wall).
  local lstate
  lstate="$(_crew_lease_state "$K")"
  if [[ "$lstate" == "DIRTY" || "$lstate" == "INCOMPLETE" ]]; then
    echo "[crew-lead] crew $K update in progress, retry (lease state $lstate)" >&2
    exit 76
  fi
  # 4. Idempotency: the owner turn's request_id keys a replay.
  local ctx="$L/.telegram_bot/inbound-context.json" receipts="$seat/runtime/lead-receipts.jsonl"
  local replay
  replay="$("$PACK_PY3" "$CL_PY" replay "$ctx" "$receipts" "$K" "$fresh" -- "$@")" \
    || _refuse "replay lookup crashed -- fail-closed"
  if "$PACK_PY3" "$CL_PY" replay-hit "$replay"; then
    return 0
  fi
  # 5. Execute in the seat as an ordinary member instance.
  local out rc=0
  mkdir -p "$seat/runtime"
  out="$(mktemp "$seat/runtime/.lead-out.XXXXXX")" || _refuse "could not create a stdout buffer in $seat/runtime"
  ( cd "$seat" && PROJECT_ROOT="$seat" DOGANY_ACTOR="$(basename "$L")" \
      DOGANY_INBOUND_CONTEXT="$ctx" exec "${argv[@]}" ) > "$out" || rc=$?
  cat "$out"
  # 6. Receipt (per seat, so receipts separate per crew like the ledgers).
  "$PACK_PY3" "$CL_PY" receipt "$receipts" "$out" "$K" "$seat" "$rc" "$replay" -- "$@" \
    || echo "[crew-lead] WARN: receipt not written ($receipts)" >&2
  rm -f "$out"
  return "$rc"
}

# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
_CHK_RED=0
_red()  { _CHK_RED=1; echo "RED  $*"; }
_warn() { echo "WARN $*"; }

# _check_leader <L> -- every section 5.5 shape for one leader root.
_check_leader() {
  local L="$1" name kits dbs
  name="$(basename "$L")"
  [[ "$(_crew_role "$L")" == "leader" ]] || _red "$L: owns seats but is not a crew leader (DOGANY_CREW_ROLE=leader)"
  kits="$(_packs_kits "$L/.instance.conf" | paste -sd, -)"
  [[ -z "$kits" ]] || _red "$L: leader root holds kit(s) in DOGANY_PACKS: $kits"
  dbs="$(cd "$L/database" 2>/dev/null && for f in *.db; do [[ -e "$f" || -L "$f" ]] && printf '%s\n' "$f"; done | paste -sd, -)"
  [[ -z "$dbs" ]] || _red "$L: leader root has database/*.db: $dbs"
  local row K seat
  while IFS= read -r row; do
    [[ -n "$row" ]] || continue
    K="${row%% *}"; seat="${row#* }"
    _check_seat "$L" "$K" "$seat"
  done < <(_seats_of_leader "$L")
}

_check_seat() {
  local L="$1" K="$2" seat="$3" n link want m owner nonseat=0 crew_seats=""
  [[ "$(_seat_phys "$(dirname "$seat")")" == "$(_seat_phys "$SEATS_DIR/$K")" && "$(basename "$seat")" == "$(basename "$L")" ]] \
    || _red "$seat: not at seats/$K/$(basename "$L")"
  [[ "$(_crew_role "$seat")" != "leader" ]] || _red "$seat: a seat claims the leader role itself"
  n="$(_packs_kits "$seat/.instance.conf" | sort -u | wc -l | tr -d ' ')"
  local kits_in
  kits_in="$(_packs_kits "$seat/.instance.conf" | sort -u | paste -sd, -)"
  if [[ "$n" -ne 1 ]]; then
    _red "$seat: holds $n kits (${kits_in:-none}) -- a seat holds exactly its one kit"
  elif [[ "$kits_in" != "$K" ]]; then
    _red "$seat: holds kit '$kits_in', not '$K'"
  fi
  # The crew DB ground truth is <shared home>/crews/<kit>/<kit>.db even when
  # DOGANY_CREW_DIR moves the registry (crew_paths.py doctrine).
  link="$seat/database/$K.db"
  want="$SHARED_HOME/crews/$K/$K.db"
  if [[ "$("$PACK_PY3" "$CL_PY" db-derived "$seat/config/packs/$K.manifest.json" 2>/dev/null)" == "1" ]]; then
    : # db_derived kits have no crew DB link; C2 is declarative there (G3)
  elif [[ ! -L "$link" ]]; then
    _red "$seat: no crew DB link (database/$K.db)"
  else
    local lt wt
    lt="$("$PACK_PY3" "$CL_PY" realpath "$link")"
    wt="$("$PACK_PY3" "$CL_PY" realpath "$want")"
    [[ "$lt" == "$wt" ]] || _red "$seat: database/$K.db points at $lt, not its own crew DB $wt"
  fi
  local other
  for other in "$seat/database"/*.db; do
    [[ -e "$other" || -L "$other" ]] || continue
    [[ "$(basename "$other")" == "$K.db" ]] || _red "$seat: holds a second DB link $(basename "$other")"
  done
  _is_member "$K" "$seat" || _red "$seat: not a member= of crew '$K'"
  ls "$seat/routines/"*.plist >/dev/null 2>&1 && _red "$seat: has routines/*.plist -- kit routines are not run from a seat (C4)"
  # Crew-level shapes: two leaders, seat-only crew (C6, C7).
  while IFS= read -r m; do
    [[ -n "$m" ]] || continue
    owner="$(_seat_of "$m")"
    if [[ -n "$owner" ]]; then
      crew_seats="$crew_seats $(_seat_phys "$owner")"
    elif [[ -d "$m" ]]; then
      nonseat=$((nonseat + 1))
    fi
  done < <(_members "$K")
  [[ "$(printf '%s\n' $crew_seats | sort -u | grep -c .)" -le 1 ]] || _red "crew '$K': seats of two leaders ($crew_seats) -- at most one secretary leader per crew (C6)"
  [[ "$nonseat" -ge 1 ]] || _red "crew '$K': only seats are members -- the crew keeps its staff (C7)"
  # WARNs: kit adoption (A-K1) and kit_dev actor role (G9). Never red.
  if ! grep -qs "DOGANY_ACTOR" "$seat/database/$K.py" "$seat/database/$K.sh" 2>/dev/null; then
    _warn "$seat: $K: DOGANY_ACTOR not adopted -- the leader is stamped by the seat basename ($(basename "$L"))"
  fi
  if ! grep -qs "DOGANY_INBOUND_CONTEXT" "$seat/database/$K.py" "$seat/database/$K.sh" 2>/dev/null; then
    _warn "$seat: $K: turn idempotency not adopted (DOGANY_INBOUND_CONTEXT) -- the runner's replay key covers the leader's retries"
  fi
  if [[ "$K" == "kit_dev" ]]; then
    local role
    role="$("$PACK_PY3" "$CL_PY" actor-role "$CREW_DIR/$K/ACTORS.md" "$(basename "$L")" 2>/dev/null)"
    [[ "$role" == "leader" ]] \
      || _warn "$seat: kit_dev ACTORS.md role for '$(basename "$L")' is '$role', not 'leader' -- seat vs actor.role drift (G9; the kit decides, dec-400)"
  fi
}

cmd_check() {
  local L=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --leader) L="${2:-}"; shift 2 || usage ;;
      *) usage ;;
    esac
  done
  _CHK_RED=0
  local leaders="" d owner
  if [[ -n "$L" ]]; then
    leaders="$(_leader_phys "$L")"
  else
    # Every seat names its owner; an owner that is gone is itself a red.
    for d in "$SEATS_DIR"/*/*; do
      [[ -d "$d" && "$(basename "$(dirname "$d")")" != "_archive" ]] || continue
      owner="$(_seat_of "$d")"
      if [[ -z "$owner" || ! -d "$owner" ]]; then
        _red "$d: seat with no live owner (DOGANY_SEAT_OF='$owner')"
        continue
      fi
      leaders="$leaders"$'\n'"$(_seat_phys "$owner")"
    done
    leaders="$(printf '%s\n' "$leaders" | grep . | sort -u)"
  fi
  local one
  while IFS= read -r one; do
    [[ -n "$one" ]] || continue
    _check_leader "$one"
  done <<< "$leaders"
  # L3 (the write-paths row) is not built yet: there is no detect log to count.
  echo "INFO L3 write-paths detect log: not built (DGN-2040 slice 6)"
  if [[ "$_CHK_RED" -eq 0 ]]; then echo "CHECK GREEN"; return 0; fi
  echo "CHECK RED"; return 1
}

VERB="${1:-}"; shift || true
case "$VERB" in
  role)   cmd_role "$@" ;;
  seat)   cmd_seat "$@" ;;
  unseat) cmd_unseat "$@" ;;
  run)    cmd_run "$@" ;;
  check)  cmd_check "$@" ;;
  *) usage ;;
esac
