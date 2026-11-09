#!/bin/bash
# crew_declare.sh -- DGN-1716: register a DECLARED crew, a crew identity
# whose kit is not installed (no shared DB, no lockstep, no shared skills).
#
# WHY. The crew registry <crews home>/<kit>/crew.conf is the one member list
# (DGN-1721): the local `dogany` launcher groups its picker by it and the
# update notice merges a crew's framework rows by it. A kit crew is joined
# by update_seed.sh, which adds a row only when the member mounts the kit
# (DOGANY_PACKS "<kit>@") AND links the crew DB. A crew whose kit is not
# mounted anywhere (e.g. a dev crew that mounts no kit) can never pass that
# seed, so it had no registry. This verb writes one, with a marker file that
# tells every crew reader what it is:
#
#   <crews home>/<kit>/crew.conf       member=<root> lines (same format)
#   <crews home>/<kit>/crew.declared   kit= / name_en= / manifest= lines
#
# WHY IT IS SAFE (the readers of */crew.conf, DGN-1716 round 4):
#   update_plan.sh / update_apply.sh   visit only MOUNTED pack ids; a kit
#       no instance mounts is never read. Mounting it later with the marker
#       still present fails closed in update_plan (convert first).
#   update_seed.sh      seeds MOUNTED kits only; refuses a declared crew.
#   crew_consent.sh / crew_lease.sh / cron-guard.sh   per kit, driven by a
#       plan or a DB symlink; a declared crew has neither.
#   crew_leave.sh       a live member of a declared crew is DECLARED (not a
#       ghost); leave removes it (the row is its whole membership).
#   mint_teardown.sh    removes the row after the archive (root gone).
#   mint_retire.py      refuses while the root is in a declared crew and
#       names the leave command.
#   mint_screens.py     a member's slug counts as taken (correct).
#   update_surface.py   members share one framework row (the DGN-1721 crew
#       shape) -- intended.
#   scripts/dogany      groups the picker under "<name_en> crew".
# The refusals below keep those facts true: the kit is not in
# pack-sources.conf, no instance mounts it, no crew DB exists for it, and an
# existing kit-crew registry (crew.conf without the marker) is never taken
# over.
#
# Usage:
#   crew_declare.sh declare --kit <kit_id> --manifest <pack-manifest.json>
#                   --member <root> [--member <root> ...] [--dry-run]
#     Member order is kept; the launcher lists the crew's first non-domain
#     member first (the leader). Re-running adds only missing members
#     (idempotent); an existing crew.conf is backed up first
#     (crew.conf.bak-declare-<ts>).
#
# Env: DOGANY_SHARED_HOME / DOGANY_CREW_DIR / DOGANY_PACK_SOURCES (layout
# override, same resolution as update_seed.sh / crew_leave.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY_RESOLVE_LIB="$SCRIPT_DIR/lib/py_resolve.sh"
[[ -f "$PY_RESOLVE_LIB" && -r "$PY_RESOLVE_LIB" ]] \
  || { echo "ERROR: python resolver library missing ($PY_RESOLVE_LIB) -- fail-closed" >&2; exit 1; }
# shellcheck source=lib/py_resolve.sh
. "$PY_RESOLVE_LIB"
pack_resolve_py3 || exit 1
# shellcheck source=lib/discover_instances.sh
. "$SCRIPT_DIR/lib/discover_instances.sh"

SHARED_HOME="${DOGANY_SHARED_HOME:-$HOME/.dogany}"
CREW_DIR="${DOGANY_CREW_DIR:-$SHARED_HOME/crews}"
SOURCES_CONF="${DOGANY_PACK_SOURCES:-$SHARED_HOME/pack-sources.conf}"

usage() {
  echo "usage: crew_declare.sh declare --kit <kit_id> --manifest <pack-manifest.json> --member <root> [--member <root> ...] [--dry-run]" >&2
  exit 2
}
die() { echo "[crew-declare] REFUSE: $*" >&2; exit 3; }

VERB="${1:-}"; shift || true
[[ "$VERB" == "declare" ]] || usage
KIT=""; MANIFEST=""; DRY=0; MEMBERS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kit) KIT="${2:-}"; shift 2 || usage ;;
    --manifest) MANIFEST="${2:-}"; shift 2 || usage ;;
    --member) MEMBERS+=("${2:-}"); shift 2 || usage ;;
    --dry-run) DRY=1; shift ;;
    *) usage ;;
  esac
done
[[ -n "$KIT" && -n "$MANIFEST" && "${#MEMBERS[@]}" -gt 0 ]] || usage
[[ "$KIT" =~ ^kit_[a-z0-9_]+$ ]] || die "kit id '$KIT' is not a kit id (kit_<name>)"

# 1. The manifest names this kit.
[[ -f "$MANIFEST" ]] || die "manifest not found: $MANIFEST"
MF="$("$PACK_PY3" - "$MANIFEST" <<'PYEOF'
import json, sys
try:
    m = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as exc:
    sys.stderr.write("unreadable manifest: %s\n" % exc)
    sys.exit(1)
print("%s\t%s\t%s" % (m.get("id", ""), m.get("kind", ""), m.get("name_en", "")))
PYEOF
)" || die "manifest is not readable JSON: $MANIFEST"
IFS=$'\t' read -r MF_ID MF_KIND NAME_EN <<< "$MF"
[[ "$MF_ID" == "$KIT" ]] || die "manifest id '$MF_ID' != --kit '$KIT'"
[[ "$MF_KIND" == "kit" ]] || die "manifest kind '$MF_KIND' is not 'kit'"
[[ -n "$NAME_EN" && "$NAME_EN" != *[!A-Za-z0-9\ _-]* ]] || die "manifest name_en '$NAME_EN' is empty or not plain ASCII"
MANIFEST="$(cd "$(dirname "$MANIFEST")" && pwd -P)/$(basename "$MANIFEST")"

# 2. The kit is not installed anywhere: not a registered pack source, no
#    crew DB, not mounted by any instance (estate scan + the named members).
if [[ -f "$SOURCES_CONF" ]] && grep -q "^$KIT\." "$SOURCES_CONF"; then
  die "$KIT is registered in $SOURCES_CONF -- an installed kit's crew is seeded by update_seed.sh, not declared"
fi
if [[ -e "$SHARED_HOME/crews/$KIT/$KIT.db" || -L "$SHARED_HOME/crews/$KIT/$KIT.db" ]]; then
  die "crew DB $SHARED_HOME/crews/$KIT/$KIT.db exists -- this is a kit crew, not a declared one"
fi
CONF="$CREW_DIR/$KIT/crew.conf"
MARK="$CREW_DIR/$KIT/crew.declared"
if [[ -f "$CONF" && ! -f "$MARK" ]]; then
  die "$CONF exists without $MARK -- a kit crew registry is never taken over"
fi
[[ ! -d "$CREW_DIR/$KIT/.lease" ]] || die "$CREW_DIR/$KIT/.lease exists -- a crew transaction owns this directory"

mounts_kit() { # mounts_kit <root>
  local packs
  packs="$(sed -n 's/^DOGANY_PACKS=//p' "$1/.instance.conf" 2>/dev/null | head -n1)"
  case ",$packs," in *",$KIT@"*) return 0 ;; esac
  return 1
}
while IFS= read -r nr; do
  [[ -n "$nr" ]] || continue
  if mounts_kit "${nr#*:}"; then
    die "${nr#*:} mounts $KIT (DOGANY_PACKS) -- update_plan would treat the crew as a kit crew"
  fi
done < <(_discover_instances || true)

# 3. Members: live instances, stored by physical path, in the given order.
ROOTS=()
for m in "${MEMBERS[@]}"; do
  [[ -d "$m" ]] || die "member not found: $m"
  phys="$(cd "$m" && pwd -P)"
  [[ -f "$phys/.instance.conf" ]] || die "member $phys has no .instance.conf -- not an instance"
  [[ -f "$phys/identity/hot.custom.agent.md" || -f "$phys/PROFILE.md" || -f "$phys/AGENT.md" ]] \
    || die "member $phys has no identity/hot.custom.agent.md, PROFILE.md or AGENT.md -- not an instance"
  mounts_kit "$phys" && die "member $phys mounts $KIT (DOGANY_PACKS) -- that is a kit member"
  [[ ! -e "$phys/database/$KIT.db" && ! -L "$phys/database/$KIT.db" ]] \
    || die "member $phys has database/$KIT.db -- that is a kit member"
  ROOTS+=("$phys")
done

echo "[crew-declare] kit: $KIT (\"$NAME_EN\", manifest $MANIFEST)"
echo "[crew-declare] registry: $CONF"
echo "[crew-declare] marker:   $MARK"
for r in "${ROOTS[@]}"; do
  if [[ -f "$CONF" ]] && grep -qxF "member=$r" "$CONF"; then
    echo "[crew-declare]   member=$r (already registered)"
  else
    echo "[crew-declare]   member=$r"
  fi
done
echo "[crew-declare] checks: not in pack-sources.conf, no crew DB, no instance mounts $KIT, members are live instances -- PASS"
if [[ "$DRY" -eq 1 ]]; then
  echo "[crew-declare] (dry-run) nothing written"
  exit 0
fi

mkdir -p "$CREW_DIR/$KIT"
"$PACK_PY3" - "$CONF" "$MARK" "$KIT" "$NAME_EN" "$MANIFEST" "${ROOTS[@]}" <<'PYEOF'
import fcntl, os, sys, tempfile, time, pathlib
conf, mark = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
kit, name_en, manifest = sys.argv[3], sys.argv[4], sys.argv[5]
roots = sys.argv[6:]

def write_atomic(path, data, mode=0o644):
    fd, tmp = tempfile.mkstemp(prefix=".declare.tmp.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

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
            sys.stderr.write("[crew-declare] FATAL: could not acquire %s within %.0fs -- nothing written\n"
                             % (lock_path, timeout))
            sys.exit(1)
        time.sleep(0.05)
try:
    # Re-check under the lock: a kit registry may have appeared meanwhile.
    if conf.exists() and not mark.exists():
        sys.stderr.write("[crew-declare] REFUSE: %s appeared without %s -- nothing written\n" % (conf, mark))
        sys.exit(3)
    text = conf.read_text(encoding="utf-8") if conf.exists() else ""
    lines = text.splitlines()
    for ln in lines:
        if ln == "" or ln.startswith("#") or ln.startswith("member="):
            continue
        sys.stderr.write("[crew-declare] FAIL: crew.conf line unparseable: '%s' -- nothing written\n" % ln)
        sys.exit(1)
    added = [r for r in roots if "member=" + r not in lines]
    write_atomic(mark, "# Declared crew (DGN-1716): a crew identity with no kit installed --\n"
                       "# no shared DB, no crew lockstep, no shared skills.\n"
                       "# Written by scripts/pack/crew_declare.sh; remove it only when the kit\n"
                       "# is installed and update_seed.sh takes the registry over.\n"
                       "kit=%s\nname_en=%s\nmanifest=%s\n" % (kit, name_en, manifest))
    if added:
        if conf.exists():
            bak = "%s.bak-declare-%s" % (conf, time.strftime("%Y%m%d-%H%M%S"))
            write_atomic(pathlib.Path(bak), text, conf.stat().st_mode & 0o7777)
            print("[crew-declare]   backup: %s" % bak)
        mode = conf.stat().st_mode & 0o7777 if conf.exists() else 0o644
        write_atomic(conf, "\n".join(lines + ["member=" + r for r in added]) + "\n", mode)
    dfd = os.open(str(conf.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    for r in added:
        print("[crew-declare]   registered: member=%s" % r)
    print("[crew-declare] done: %s, %d member(s) added" % (kit, len(added)))
finally:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()
PYEOF
