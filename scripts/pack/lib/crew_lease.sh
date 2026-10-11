# crew_lease.sh -- DGN-1509 SPEC v2 §4 crew-transaction lease primitive
# (pessimistic write). Sourced (no shebang -- a library, not a runnable
# script). Requires $CREW_DIR and $PACK_PY3 to already be set by the caller
# (both are resolved by every sourcing script before this file is loaded).
#
# WHY THIS EXISTS (DGN-1509 B2/G4). update_apply.sh's crew-atomic transaction
# (_apply_crew) stops every member bridge, migrates the ONE shared kit DB,
# then restarts every bridge. A signal (TERM/INT/HUP) that arrives DURING
# that window used to be invisible to the EXIT-trap re-arm: the trap's TWO
# revival arms, _flush_deferred (deferred framework restarts) and
# _rearm_stopped (stop-debt re-arm), each only consulted the in-PROCESS
# variable DOWN_ROOTS, which is populated by the caller ONLY AFTER
# _apply_crew returns (update_apply.sh _mark_down call sites) -- a signal
# that kills the process mid-transaction never reaches that assignment, so
# DOWN_ROOTS stays empty and either arm could re-arm a member bridge on top
# of a possibly half-migrated shared DB. This file gives update_apply.sh a
# DURABLE, on-disk record of "a crew transaction is mutating this kit's
# shared DB right now" that survives the process dying -- BOTH arms
# (_flush_deferred, _rearm_stopped in update_apply.sh) read it as a SECOND
# stay-down input alongside DOWN_ROOTS.
#
# Layout: $CREW_DIR/<kit>/.lease/
#   holder    pid=<pid>\ntxn=<txn>\nstarted_at=<epoch>\nhost=<host>
#   state     DIRTY | INCOMPLETE   (see below -- no PREPARING/other split)
#   members   one instance root per line (the crew this lease protects)
#   reason    (INCOMPLETE only) human-readable failure line
#
# DIRTY is fsync'd after stop/backup and BEFORE the first possible payload or
# shared-DB mutation (update_apply.sh T0 documents this boundary). Normal
# completion removes the lease; failures retain it. Explicit recovery through
# update_apply.sh --clear-incomplete <kit> verifies every member and archives
# the lease together with an audit receipt. No expiry or automatic reclaim.
# Re-arm readers select only this run's acquisitions: refusing an older lease
# cannot turn this run's zero-write stop debt into a permanent outage.

# Per-process acquisition identity distinguishes this run from PID reuse.
# Consumers filter by it before holding their own stop debt.
CREW_LEASE_RUN_ID="$$-$(date +%s)-${RANDOM}-${RANDOM}"

_crew_lease_dir() { # <kit>
  printf '%s/%s/.lease' "$CREW_DIR" "$1"
}

# _crew_lease_state <kit> -- prints DIRTY / INCOMPLETE / '' (no lease).
_crew_lease_state() {
  local f
  f="$(_crew_lease_dir "$1")/state"
  if [[ -f "$f" ]]; then
    tr -d '[:space:]' < "$f" 2>/dev/null
  else
    printf ''
  fi
}

# _crew_lease_active <kit> -- rc 0 iff a lease directory exists at all
# (any state) -- used by participants that must refuse OUTRIGHT while a
# crew transaction owns this kit (spec §4 participant list, e.g. M3).
_crew_lease_active() {
  [[ -d "$(_crew_lease_dir "$1")" ]]
}

# _crew_lease_holder_field <kit> <field> -- prints the holder file's
# <field>= value, or '' if no lease/holder file exists. Single parse point
# for every holder reader below (DGN-1517 §2.2 "파싱 중복 금지").
_crew_lease_holder_field() {
  local f
  f="$(_crew_lease_dir "$1")/holder"
  [[ -f "$f" ]] || { printf ''; return 0; }
  grep -E "^$2=" "$f" 2>/dev/null | head -n1 | cut -d= -f2-
}

# _crew_lease_holder_txn <kit> -- the holder's txn= value. Used by
# DGN-1517's crew-lockstep guard evidence class (E1): a lease existing is
# not enough on its own -- the holder's txn must equal the SOURCE_TAG the
# current install is carrying, proving this is the SAME transaction, not a
# stale/unrelated one.
_crew_lease_holder_txn() { _crew_lease_holder_field "$1" txn; }

# _crew_lease_holder_pid / _crew_lease_holder_started_at -- consumed by the
# guard's lease-freshness disarm (DGN-1517 F-1 secondary check): a DIRTY
# lease is a latch with no expiry (released only by verified recovery), so
# the guard bounds how long a dead-holder lease may keep authorizing
# evidence class 3a.
_crew_lease_holder_pid()        { _crew_lease_holder_field "$1" pid; }
_crew_lease_holder_started_at() { _crew_lease_holder_field "$1" started_at; }

# _crew_lease_has_member <kit> <phys_root> -- rc 0 iff the lease's members
# file lists a root whose OWN physical path (cd -P) equals <phys_root>.
# Canonicalizes only the members-file side -- <phys_root> is expected to
# already be physical (the guard's scan is discover_instances.sh's own
# pwd -P output). This asymmetry matters: crew.conf/members entries are
# literal strings (update_apply.sh:1669), so an alias root (agents/main ->
# agents/<leader>, discover_instances.sh's own documented shape) would
# otherwise miss a real member and fall through to the safe-direction
# over-block (DGN-1517 spec v1 §2.2 C-1) rather than silently matching.
_crew_lease_has_member() {
  local kit="$1" phys_root="$2" f line lp
  f="$(_crew_lease_dir "$kit")/members"
  [[ -f "$f" ]] || return 1
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    lp="$(cd -P "$line" 2>/dev/null && pwd)" || continue
    [[ "$lp" == "$phys_root" ]] && return 0
  done < "$f"
  return 1
}

# _crew_lease_write_dirty <kit> <txn> <members-newline-list>
# Pessimistic write: fails loud (rc 1, nothing written) if a lease already
# exists in DIRTY/INCOMPLETE state -- that means a prior transaction never
# completed and was never cleared; racing a second write over it would lose
# the first failure's record. Fsyncs each file AND the directory before
# returning, so the write survives a crash immediately after this call.
_crew_lease_write_dirty() {
  local kit="$1" txn="$2" members="$3"
  local dir existing
  dir="$(_crew_lease_dir "$kit")"
  existing="$(_crew_lease_state "$kit")"
  if [[ "$existing" == "DIRTY" || "$existing" == "INCOMPLETE" ]]; then
    echo "[crew-lease] kit '$kit': existing lease state='$existing' at $dir -- a prior transaction was never cleared (--clear-incomplete not run), refusing to start a new one over it (fail-closed, DGN-1509 B2)" >&2
    printf '[crew-lease] Verify/release after forward repair: bash %q --clear-incomplete %q\n' "${BASH_SOURCE[0]%/*}/../update_apply.sh" "$kit" >&2
    return 1
  fi
  "$PACK_PY3" - "$dir" "$$" "$txn" "$(date +%s)" "$(hostname 2>/dev/null || echo unknown)" "$members" "$CREW_LEASE_RUN_ID" <<'PYEOF'
import os, sys, tempfile
dir_, pid, txn, started_at, host, members, run_id = sys.argv[1:8]
os.makedirs(os.path.dirname(dir_), exist_ok=True)
os.mkdir(dir_)  # Exclusive acquisition: never overwrite even an unknown/partial lease.


def atomic_write(name, data):
    fd, tmp = tempfile.mkstemp(prefix="." + name + ".tmp.", dir=dir_)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, os.path.join(dir_, name))


atomic_write("holder", "pid=%s\ntxn=%s\nstarted_at=%s\nhost=%s\nrun_id=%s\n" % (pid, txn, started_at, host, run_id))
mtext = members
if mtext and not mtext.endswith("\n"):
    mtext += "\n"
atomic_write("members", mtext)
# state written LAST -- readers treat its presence/value as the verdict, so
# holder/members are already durable on disk by the time state says DIRTY.
atomic_write("state", "DIRTY\n")

dfd = os.open(dir_, os.O_RDONLY)
try:
    os.fsync(dfd)
finally:
    os.close(dfd)
PYEOF
}

# _crew_lease_mark_incomplete <kit> <reason> -- DIRTY -> INCOMPLETE. Requires
# an existing lease directory (a transaction that never wrote DIRTY has
# nothing to transition -- rc 1, loud). Recovery is the explicit verifying
# update_apply.sh --clear-incomplete <kit> entrypoint.
_crew_lease_mark_incomplete() {
  local kit="$1" reason="$2" dir
  dir="$(_crew_lease_dir "$kit")"
  if [[ ! -d "$dir" ]]; then
    echo "[crew-lease] kit '$kit': mark-incomplete called with no lease directory at $dir -- nothing to transition" >&2
    return 1
  fi
  "$PACK_PY3" - "$dir" "$reason" <<'PYEOF'
import os, sys, tempfile
dir_, reason = sys.argv[1], sys.argv[2]


def atomic_write(name, data):
    fd, tmp = tempfile.mkstemp(prefix="." + name + ".tmp.", dir=dir_)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, os.path.join(dir_, name))


atomic_write("reason", reason + "\n")
atomic_write("state", "INCOMPLETE\n")

dfd = os.open(dir_, os.O_RDONLY)
try:
    os.fsync(dfd)
finally:
    os.close(dfd)
PYEOF
}

# _crew_lease_clear <kit> -- remove the lease directory. Callers MUST only
# call this on a VERIFIED completion (spec §4 "해제 규칙"); never call it on
# an INCOMPLETE transition path; recovery uses _crew_lease_verify_clear.
_crew_lease_clear() {
  rm -rf "$(_crew_lease_dir "$1")"
}

# _crew_lease_dirty_member_roots -- union (space-separated, deduped) of every
# member root listed under a lease whose state is DIRTY or INCOMPLETE,
# across ALL kits under $CREW_DIR. Optional pid/run-id filters select only
# acquisitions by the calling run (pre-write refusals owe their stops back).
# This is the read side BOTH _on_exit
# revival arms consume (_flush_deferred and _rearm_stopped): a root in this
# set stays down regardless of DOWN_ROOTS (spec §7 stay_down formula --
# G4: the formula applies to both arms, not just one). Empty when
# $CREW_DIR has no kit with an active lease (today, always -- nothing but
# update_apply.sh's own crew transaction ever writes one, and it always
# clears on success).
_crew_lease_dirty_member_roots() {
  local d kit state f line roots="" holder_pid="${1:-}" run_id="${2:-}"
  [[ -d "$CREW_DIR" ]] || { printf ''; return 0; }
  for d in "$CREW_DIR"/*/.lease; do
    [[ -d "$d" ]] || continue
    kit="$(basename "$(dirname "$d")")"
    if [[ -n "$holder_pid" && "$(_crew_lease_holder_pid "$kit")" != "$holder_pid" ]]; then
      continue
    fi
    if [[ -n "$run_id" && "$(_crew_lease_holder_field "$kit" run_id)" != "$run_id" ]]; then
      continue
    fi
    state="$(_crew_lease_state "$kit")"
    case "$state" in
      DIRTY|INCOMPLETE) : ;;
      *) continue ;;
    esac
    f="$d/members"
    [[ -f "$f" ]] || continue
    while IFS= read -r line; do
      [[ -n "$line" ]] || continue
      case " $roots " in
        *" $line "*) : ;;
        *) roots="${roots:+$roots }$line" ;;
      esac
    done < "$f"
  done
  printf '%s' "$roots"
}

# Explicit recovery only. Archive the guard and assertions together; never
# delete evidence. A live/foreign holder or incomplete proof keeps the latch.
_crew_lease_verify_clear() {
  "$PACK_PY3" - "$CREW_DIR" "$SHARED_HOME" "$1" \
    "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install_evidence.sh" <<'PYEOF'
import datetime, fcntl, getpass, json, os, pathlib, re, socket, sqlite3, subprocess, sys, uuid
crew_dir, shared_home, kit, evidence_lib = sys.argv[1:]
lease = pathlib.Path(crew_dir) / kit / '.lease'
lock_fd = None

def require(ok, message):
    if not ok:
        raise ValueError(message)

def fields(path):
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        require(sep and key not in result, 'malformed/duplicate field in ' + str(path))
        result[key] = value.strip().strip('"\'')
    return result

try:
    require(re.fullmatch(r'[A-Za-z0-9_-]+', kit), 'invalid kit')
    require(lease.is_dir() and not lease.is_symlink(), 'missing or symlink lease')
    # Kernel lock releases even on SIGKILL; recovery must not add another latch.
    lock_fd = os.open(lease / 'holder', os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = (lease / 'state').read_text().strip()
    require(state in ('DIRTY', 'INCOMPLETE'), 'unknown lease state')
    holder = fields(lease / 'holder')
    require(holder.get('host') == socket.gethostname(), 'foreign/unknown holder host')
    require(holder.get('txn') and holder.get('started_at', '').isdigit(), 'unknown transaction')
    require(holder.get('pid', '').isdigit() and int(holder['pid']) > 0, 'unknown holder pid')
    try:
        os.kill(int(holder['pid']), 0)
    except ProcessLookupError:
        pass
    else:
        raise ValueError('holder is still alive')
    members = [pathlib.Path(p).resolve(strict=True) for p in (lease / 'members').read_text().splitlines() if p]
    registered = []
    for line in (lease.parent / 'crew.conf').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        require(line.startswith('member='), 'malformed crew.conf')
        registered.append(pathlib.Path(line[7:]).resolve(strict=True))
    require(members and len(set(members)) == len(members) and set(members) == set(registered),
            'lease/registry member set mismatch')
    db = (pathlib.Path(shared_home) / 'crews' / kit / (kit + '.db')).resolve(strict=True)
    con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=0)
    con.execute('BEGIN')
    uv = con.execute('PRAGMA user_version').fetchone()[0]
    require(con.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'DB quick_check failed')
    assertions = []
    versions = set()
    txn_match = re.fullmatch(r'pack/([^/]+)/v(.+)', holder['txn'])
    require(txn_match, 'unrecognized transaction tag')
    pack = txn_match[1]
    require(re.fullmatch(r'[A-Za-z0-9_-]+', pack), 'invalid transaction pack')
    for root in members:
        require((root / 'database' / (kit + '.db')).resolve(strict=True) == db, 'member DB target mismatch')
        instance = fields(root / '.instance.conf')
        entries = [v.split('@', 1)[1] for v in instance.get('DOGANY_PACKS', '').split(',')
                   if v.startswith(pack + '@')]
        require(len(entries) == 1 and entries[0], 'missing/ambiguous ledger version')
        result = subprocess.run(['bash', '-c', '. "$1"; evidence_read "$2" "$3" "$4"',
                                 'verify', evidence_lib, str(root), pack, entries[0]],
                                capture_output=True, text=True)
        require(result.returncode == 0 and ' verdict=VERIFIED ' in result.stdout,
                'delivery evidence refused: ' + result.stdout.strip())
        versions.add(entries[0])
        code = (root / 'database' / (kit + '.py')).read_text()
        pins = re.findall(r'^EXPECTED_USER_VERSION\s*=\s*([0-9]+)\s*(?:#.*)?$', code, re.M)
        require(len(pins) == 1 and int(pins[0]) == uv, 'code/DB pin mismatch')
        conf_pins = []
        for conf in (root / 'config' / 'agent.conf', root / 'config' / (kit + '.conf')):
            if conf.exists():
                data = fields(conf)
                if 'L1_EXPECTED_USER_VERSION' in data:
                    conf_pins.append(data['L1_EXPECTED_USER_VERSION'])
        require(conf_pins and all(p.isdigit() and int(p) == uv for p in conf_pins), 'conf/DB pin mismatch')
        assertions.append(dict(root=str(root), version=entries[0], receipt='VERIFIED',
                               code_pin=int(pins[0]), conf_pins=conf_pins, db=str(db)))
    require(len(versions) == 1, 'crew receipt versions disagree')
    receipt = dict(schema=1, action='clear-incomplete', who=getpass.getuser(), uid=os.getuid(),
                   host=socket.gethostname(), when=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   holder=holder, prior_state=state, db_user_version=uv, quick_check='ok', members=assertions)
    # A killed verifier may leave a partial/past receipt. Re-verify first,
    # then atomically replace it; recovery itself must remain retryable.
    receipt_tmp = lease / ('.clear-receipt-' + uuid.uuid4().hex)
    with receipt_tmp.open('x') as out:
        json.dump(receipt, out, indent=2)
        out.write('\n'); out.flush(); os.fsync(out.fileno())
    os.replace(receipt_tmp, lease / 'clear-receipt.json')
    fd = os.open(lease, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    archive = lease.parent / ('.lease-cleared-' + uuid.uuid4().hex)
    os.rename(lease, archive)
    fd = os.open(archive.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    con.close()
    print('[crew-lease] verified recovery: ' + str(archive / 'clear-receipt.json'))
except (OSError, ValueError, sqlite3.Error) as error:
    print('[crew-lease] REFUSE: ' + str(error), file=sys.stderr)
    sys.exit(2)
finally:
    if lock_fd is not None:
        os.close(lock_fd)
PYEOF
}
