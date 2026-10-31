# crew_leave.sh -- DGN-1543 R8: the crew's missing leave verb.
# Sourced (no shebang -- a library, not a runnable script), same contract as
# its siblings lib/crew_lease.sh / lib/crew_consent.sh: requires $PACK_PY3 to
# already be resolved by the caller. $SHARED_HOME / $CREW_DIR are respected
# when the caller already set them and defaulted otherwise (mint_teardown.sh
# sources this file without owning either name).
#
# WHY THIS EXISTS (DGN-1543 R8, measured). Every crew script was a forward
# verb -- join existed (DGN-1509 S2: pack_install.sh physical join +
# update_seed.sh administrative join), leave did not. When an instance was
# torn down its `member=<root>` row stayed in crew.conf as a ghost pointing
# at a dead path, and a human deleted it by hand -- three times
# (crew.conf.bak-wargremove-20260915-055922, .bak-warg2-20260915-073801,
# .bak-healthremove-20260917-180218). Owner ruling 2026-09-16 12:08:
# "if 'someone does it by hand' appears in a solution, it is not a solution".
#
# THE PREDICATE (the exact inverse of update_seed.sh's §3 seed predicate,
# stated so the next reader can disagree with a fact, not a feeling).
# Seed ADDS member=<root> to <kit>/crew.conf only when BOTH physical
# membership sources hold:
#   (a) readlink <root>/database/<kit>.db == $SHARED_HOME/crews/<kit>/<kit>.db
#   (b) <root>/.instance.conf DOGANY_PACKS carries a "<kit>@" entry
# Leave REMOVES the row only when NEITHER holds (the root is gone, or it is
# present with zero physical membership signals). The middle cell -- exactly
# one of (a)/(b) still true -- is DRIFT: removing the registry row there
# would either create the unregistered-sharer state update_apply.sh T0-1d
# fail-closes on (symlink still live) or silently bless a half-broken join.
# Neither is this verb's call to make: DRIFT is REFUSED with the surviving
# signal named. A FULL member is refused outright -- deregistering a live
# member is teardown's job first (mint_run.sh teardown archives the body,
# THEN calls this verb on the now-dead root).
#
# WHAT LEAVE NEVER TOUCHES (removal of membership is not destruction of
# records -- DGN-1543 R12 doctrine: the shared kit DB is crew property):
#   - $SHARED_HOME/crews/<kit>/<kit>.db (+-wal/-shm)  never deleted, never
#     moved, even when the LAST member leaves -- the crew dir outlives its
#     membership on purpose.
#   - <kit>/.consent/*  a stale token self-invalidates: its member-set
#     binding compares against the CURRENT crew.conf, which this verb just
#     changed -> MEMBER-DRIFT refusal + re-ask lane (lib/crew_consent.sh).
#   - <kit>/.lease/*    owned by a crew transaction. While a lease exists
#     this verb REFUSES the whole kit (same rule as update_seed.sh M3:
#     participants refuse outright, they do not wait).
#   - the instance root itself (conf, symlink, DB copies) -- leave is the
#     ADMINISTRATIVE unjoin only. The physical unjoin of a live instance
#     (unlink database/<kit>.db + drop the DOGANY_PACKS entry) would be a
#     kit-uninstall verb that does not exist yet; refusing beats half-doing.
#
# BACKUP: the existing convention, not a new one -- <file>.bak-<tag>-<ts>
# (same family as update_apply.sh T0-8 "$crew_db.t0.v${v}.bak-$ts" and the
# three hand-edit backups this ticket measured). The backup is written
# INSIDE the same flock that rewrites crew.conf, so it is byte-for-byte the
# file the removal replaced.

_CL_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Respect a caller's already-resolved layout; default the rest. ${VAR:=} is
# deliberate -- mint_run.sh sources this transitively and owns neither name.
: "${SHARED_HOME:=${DOGANY_SHARED_HOME:-$HOME/.dogany}}"
: "${CREW_DIR:=${DOGANY_CREW_DIR:-$SHARED_HOME/crews}}"

# Lease reads ride the one existing implementation (crew_lease.sh reads
# $CREW_DIR at call time, so the ordering above matters).
if ! declare -F _crew_lease_active >/dev/null 2>&1; then
  # shellcheck source=crew_lease.sh
  . "$_CL_LIB_DIR/crew_lease.sh"
fi

_cl_phys() { # _cl_phys <path> -- physical dir path, empty when unresolvable
  local p="$1"
  [[ -n "$p" && -d "$p" ]] || { printf ''; return 0; }
  (cd "$p" 2>/dev/null && pwd -P) || true
}

# _crew_member_verdict <kit> <root> -- one line on stdout:
#   OK                              full physical member (seed would re-add)
#   GHOST <detail>                  zero physical membership signals
#   DRIFT <detail>                  exactly one signal survives (refuse lane)
#   DECLARED                        live member of a DECLARED crew (DGN-1716,
#                                   crew.declared beside crew.conf: no kit, so
#                                   no physical signal exists; the row is the
#                                   whole membership and leave may remove it)
# READ-ONLY. The symlink ground truth is ALWAYS $SHARED_HOME/crews (same
# rule as update_seed.sh: DOGANY_CREW_DIR redirects only where crew.conf is
# written, never what physical membership means).
_crew_member_verdict() {
  local kit="$1" root="$2" link target packs link_ok=0 packs_ok=0
  if [[ ! -e "$root" ]]; then
    echo "GHOST root-missing"
    return 0
  fi
  target="$SHARED_HOME/crews/$kit/$kit.db"
  link="$(readlink "$root/database/$kit.db" 2>/dev/null || echo "")"
  [[ -n "$link" && "$link" == "$target" ]] && link_ok=1
  packs="$(sed -n 's/^DOGANY_PACKS=//p' "$root/.instance.conf" 2>/dev/null | head -n1)"
  case ",$packs," in *",$kit@"*) packs_ok=1 ;; esac
  if [[ -f "$CREW_DIR/$kit/crew.declared" && "$link_ok" -eq 0 && "$packs_ok" -eq 0 ]]; then
    if [[ -f "$root/.instance.conf" ]]; then
      echo "DECLARED"
    else
      echo "GHOST not-an-instance (declared crew; root has no .instance.conf)"
    fi
    return 0
  fi
  if [[ "$link_ok" -eq 1 && "$packs_ok" -eq 1 ]]; then
    echo "OK"
  elif [[ "$link_ok" -eq 0 && "$packs_ok" -eq 0 ]]; then
    echo "GHOST no-physical-membership (root exists; symlink and DOGANY_PACKS both gone)"
  elif [[ "$link_ok" -eq 1 ]]; then
    echo "DRIFT symlink still resolves to $target but DOGANY_PACKS has no $kit@ entry"
  else
    echo "DRIFT DOGANY_PACKS still carries $kit@ but database/$kit.db does not resolve to $target (readlink: ${link:-absent})"
  fi
}

# _crew_leave_matches <root> [kit] -- prints "<kit> <stored_root>" for every
# crew.conf row registering <root>, matched against the row as stored, the
# root as given, and both sides' physical paths (macOS /var vs /private/var
# spellings must not hide a membership -- same doctrine as
# mint_teardown_liveness's root matching).
_crew_leave_matches() {
  local root="$1" only_kit="${2:-}" phys d kit conf line stored sphys
  phys="$(_cl_phys "$root")"
  for d in "$CREW_DIR"/*/; do
    [[ -d "$d" ]] || continue
    kit="$(basename "$d")"
    [[ -z "$only_kit" || "$kit" == "$only_kit" ]] || continue
    conf="$d/crew.conf"
    [[ -f "$conf" ]] || continue
    while IFS= read -r line; do
      case "$line" in member=*) ;; *) continue ;; esac
      stored="${line#member=}"
      if [[ "$stored" == "$root" || ( -n "$phys" && "$stored" == "$phys" ) ]]; then
        echo "$kit $stored"; continue
      fi
      sphys="$(_cl_phys "$stored")"
      if [[ -n "$sphys" && ( "$sphys" == "$root" || ( -n "$phys" && "$sphys" == "$phys" ) ) ]]; then
        echo "$kit $stored"
      fi
    done < "$conf"
  done
}

# _crew_leave_one <kit> <stored_root> <tag> <dry_run>
# Removes member=<stored_root> from <kit>/crew.conf behind the predicate
# above. rc 0 = removed or already absent (idempotent, like seed), rc 3 =
# refused (FULL member or DRIFT -- the registry row is still load-bearing),
# rc 1 = fail-closed (lease held / suspect registry / write failure).
# On removal prints a machine line:  LEFT <kit> <backup-path>
_crew_leave_one() {
  local kit="$1" stored="$2" tag="$3" dry="$4"
  local conf="$CREW_DIR/$kit/crew.conf" verdict ts bak n=0
  tag="${tag//\//-}"

  if _crew_lease_active "$kit"; then
    echo "[crew-leave] FAIL: 크루 $kit -- lease 보유 중 (state=$(_crew_lease_state "$kit")) -- crew.conf 탈퇴 거부 (update_seed.sh M3와 동일 규칙, fail-closed). 트랜잭션 종료 후 재실행하세요" >&2
    return 1
  fi

  verdict="$(_crew_member_verdict "$kit" "$stored")"
  case "$verdict" in
    OK)
      echo "[crew-leave] REFUSE: $kit member $stored is still a FULL physical member (symlink + DOGANY_PACKS both live) -- removing only the registry row would create the unregistered-sharer state update_apply.sh T0-1d fail-closes on. Retire the completed instance first (mint_run.sh retire --root ROOT), with its units already stopped, or do not leave." >&2
      return 3 ;;
    DRIFT*)
      echo "[crew-leave] REFUSE: $kit member $stored has PARTIAL physical membership -- ${verdict#DRIFT }. One signal survives, so this row may still be load-bearing; repair the join (update_seed.sh path) or tear the instance down. Leave refuses to guess." >&2
      return 3 ;;
    GHOST*|DECLARED) : ;;
    *)
      echo "[crew-leave] FAIL: unrecognized member verdict '$verdict' -- fail-closed" >&2
      return 1 ;;
  esac

  if [[ "$dry" -eq 1 ]]; then
    echo "[crew-leave] (dry-run) would remove 'member=$stored' from $conf (backup first: $conf.bak-leave-$tag-<ts>) -- verdict: $verdict"
    return 0
  fi

  ts="$(date +%Y%m%d-%H%M%S)"
  bak="$conf.bak-leave-$tag-$ts"
  while [[ -e "$bak" ]]; do n=$((n + 1)); bak="$conf.bak-leave-$tag-$ts-$n"; done

  "$PACK_PY3" - "$conf" "member=$stored" "$bak" "$kit" <<'PYEOF'
import fcntl, os, sys, tempfile, time, pathlib
conf = pathlib.Path(sys.argv[1])
line, bak, kit = sys.argv[2], sys.argv[3], sys.argv[4]
if not conf.exists():
    print("[crew-leave]   no crew.conf at %s -- nothing to leave (no-op)" % conf)
    sys.exit(0)
lock_path = str(conf) + ".lock"
timeout = float(os.environ.get("DOGANY_CONF_LOCK_TIMEOUT", "30"))
lock_fh = open(lock_path, "a")
deadline = time.monotonic() + timeout
while True:
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.monotonic() >= deadline:
            sys.stderr.write(
                "[crew-leave] FATAL: could not acquire %s within %.0fs "
                "(concurrent writer stuck?) -- conf untouched\n" % (lock_path, timeout))
            sys.exit(1)
        time.sleep(0.05)
try:
    text = conf.read_text(encoding="utf-8")
    lines = text.splitlines()
    if line not in lines:
        print("[crew-leave]   not registered, nothing removed: %s" % line)
        sys.exit(0)
    # Fail-closed on a suspect registry (same rule as crew_consent record /
    # update_plan T0-1): rewriting a file we cannot fully parse is guessing.
    for ln in lines:
        if ln == "" or ln.startswith("#") or ln.startswith("member="):
            continue
        sys.stderr.write("[crew-leave] FAIL: crew.conf line unparseable: '%s' "
                         "-- refusing to rewrite a suspect registry (fail-closed)\n" % ln)
        sys.exit(1)
    # Backup INSIDE the lock: byte-for-byte the content the removal replaces.
    fd, tmpb = tempfile.mkstemp(prefix=".leave.bak.tmp.", dir=str(conf.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmpb, bak)
    kept = [ln for ln in lines if ln != line]
    data = ("\n".join(kept) + "\n") if kept else ""
    mode = conf.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=".leave.tmp.", dir=str(conf.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, conf)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(str(conf.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    print("[crew-leave]   removed: %s (members left: %d)"
          % (line, sum(1 for ln in kept if ln.startswith("member="))))
    print("[crew-leave]   backup: %s" % bak)
    print("LEFT %s %s" % (kit, bak))
finally:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()
PYEOF
}

# _crew_rejoin_upsert <kit> <root> -- restore's inverse: re-append
# member=<root> if absent. Same flock/atomic family as update_seed.sh
# _line_upsert (append-if-absent, never a rewrite of other lines), used ONLY
# by mint_teardown_restore to put a receipt-recorded membership back.
_crew_rejoin_upsert() {
  local kit="$1" root="$2"
  local conf="$CREW_DIR/$kit/crew.conf"
  "$PACK_PY3" - "$conf" "member=$root" <<'PYEOF'
import fcntl, os, sys, tempfile, time, pathlib
conf = pathlib.Path(sys.argv[1])
line = sys.argv[2]
lock_path = str(conf) + ".lock"
timeout = float(os.environ.get("DOGANY_CONF_LOCK_TIMEOUT", "30"))
conf.parent.mkdir(parents=True, exist_ok=True)
lock_fh = open(lock_path, "a")
deadline = time.monotonic() + timeout
while True:
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.monotonic() >= deadline:
            sys.stderr.write("[crew-leave] FATAL: could not acquire %s within %.0fs -- conf untouched\n"
                             % (lock_path, timeout))
            sys.exit(1)
        time.sleep(0.05)
try:
    lines = conf.read_text(encoding="utf-8").splitlines() if conf.exists() else []
    if line in lines:
        print("[crew-leave]   already registered, untouched: %s" % line)
        sys.exit(0)
    lines.append(line)
    data = "\n".join(lines) + "\n"
    mode = conf.stat().st_mode & 0o7777 if conf.exists() else 0o644
    fd, tmp = tempfile.mkstemp(prefix=".rejoin.tmp.", dir=str(conf.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, conf)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(str(conf.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    print("[crew-leave]   re-registered: %s" % line)
finally:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()
PYEOF
}

# _crew_leave_scan [kit] -- detection HALF, deliberately separate from
# removal (DGN-1543 R8 item 4: report ghosts, never auto-delete on sight).
# Prints one verdict line per registered member. rc 0 = every row OK,
# rc 10 = at least one GHOST/DRIFT found, rc 1 = suspect registry.
_crew_leave_scan() {
  local only_kit="${1:-}" d kit conf line stored verdict findings=0 rows=0 suspect=0
  for d in "$CREW_DIR"/*/; do
    [[ -d "$d" ]] || continue
    kit="$(basename "$d")"
    [[ -z "$only_kit" || "$kit" == "$only_kit" ]] || continue
    conf="$d/crew.conf"
    [[ -f "$conf" ]] || continue
    while IFS= read -r line; do
      case "$line" in
        member=*) ;;
        ''|'#'*) continue ;;
        *)
          echo "[crew-leave] FAIL: 크루 $kit crew.conf 라인 파싱 실패: '$line' -- fail-closed" >&2
          suspect=1; continue ;;
      esac
      stored="${line#member=}"
      rows=$((rows + 1))
      verdict="$(_crew_member_verdict "$kit" "$stored")"
      case "$verdict" in
        OK|DECLARED) echo "[crew-leave] $kit member $stored -> $verdict" ;;
        GHOST*)
          echo "[crew-leave] $kit member $stored -> $verdict"
          echo "[crew-leave]   ghost row -- remove with: scripts/pack/crew_leave.sh leave --root $stored --kit $kit"
          findings=$((findings + 1)) ;;
        DRIFT*)
          echo "[crew-leave] $kit member $stored -> $verdict"
          echo "[crew-leave]   drift -- one membership source survives; repair the join (DGN-1509 two-step: DOGANY_CREW_JOIN_ACK=1 pack_install.sh, then scripts/pack/update_seed.sh) or tear the instance down. Not auto-removable."
          findings=$((findings + 1)) ;;
      esac
    done < "$conf"
  done
  echo "[crew-leave] scan: 등록 멤버 ${rows}건, 이상 ${findings}건 (탐지는 보고만 합니다 -- 자동 삭제 없음)"
  [[ "$suspect" -eq 0 ]] || return 1
  [[ "$findings" -eq 0 ]] || return 10
  return 0
}
