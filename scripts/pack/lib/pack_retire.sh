# pack_retire.sh -- pack-level retirement / absorption (DGN-1922).
# Sourced library (pack_install.sh --retire is the one caller; the
# direct-execution guard at the bottom adds a read-only `declaration` CLI the
# tests use). Requires python3 (PACK_PY3, py_resolve.sh) and the DGN-1422
# skill_retire.sh functions (skill_retire_other_owner / _plan / _apply),
# which pack_install.sh already sources before this file.
#
# THE GAP THIS CLOSES: install (pack_install.sh) and skill-level retire
# (DGN-1422) exist, but nothing could take a whole pack OFF an instance. A
# pack absorbed into a kit (pack_ledger -> kit_life 2.46.0 module_ledger)
# kept its DOGANY_PACKS entry, its config/packs/<id>.* records (so it stayed
# in every install's skill-collision universe and in update_plan), and its
# service/, config and routine files stayed on disk.
#
# DECLARATION (catalog row, read by update_plan.sh and here -- one parser,
# pack_retire_declaration below):
#   { "id": "pack_ledger", "status": "retired",
#     "absorbed_by": { "kit": "kit_life", "range": ">=2.46.0" } }
# The row stays in the catalog for the record; status "retired" takes it out
# of the publish universe (skill_collision_check default filter) and the
# install-side D-D signature gate refuses a fresh install of it (catalog
# status != manifest status).
#
# WHAT ONE RETIRE DOES (never a bare rm -- everything is MOVED under one
# archive dir, mirroring its instance-relative path, so an operator restore
# is a plain move back):
#   0. gate: the declaration is well-formed AND the instance records the
#      absorbing kit at a version inside absorbed_by.range (DOGANY_PACKS +
#      install evidence VERIFIED/UNKNOWN). Otherwise refuse, zero writes.
#   1. skills declared in config/packs/<id>.skills: each name still held by
#      another record (skill_retire_other_owner -- the absorbing kit usually
#      ships the same name) is LEFT; the rest go through skill_retire_apply
#      (archive under .claude/skills-retired/, the DGN-1422 lane).
#   2. files recorded in config/packs/<id>.files (+ receipt anchors), plus
#      config/<id>.conf (the pack's own merge-key conf, a "# merged:" row):
#      a path also recorded by ANOTHER config/packs/*.files ledger is LEFT
#      (now owned by that record); skill paths are step 1's; the rest move to
#      <archive>/files/<path>. Paths are validated (relative, no '..').
#   3. hook commands: a settings(.local).json hook command that runs one of
#      the files step 2 archives is unwired (no other record owns that
#      script, so no other manifest declares it); the removed entries are
#      saved to <archive>/hooks-removed.json.
#   4. DOGANY_PACKS / DOGANY_PACK_PINS entry for <id> dropped (atomic, same
#      flock + os.replace shape as pack_install _packs_upsert_atomic).
#   5. config/packs/<id>.* moved to <archive>/config-packs/ -- the collision
#      gate, the reqfw guard and update_plan stop seeing the id.
#   6. receipt: <root>/.claude/packs-retired/<id>.retired (key=value).
# Archive: <root>/.claude/packs-retired/<id>.<ts>/ (next to the DGN-1422
# .claude/skills-retired/ precedent; update.sh never rsyncs --delete there).
#
# Steps are ordered so a run killed half-way re-runs cleanly: the records
# that name what to move (.files/.skills) are moved LAST, and "mounted" is
# DOGANY_PACKS entry OR any config/packs/<id>.* record.

# pack_retire_declaration <catalog> <id>
# Prints "<status>\t<kit>\t<range>" for the row (status "" = no row).
# rc 0 = row read; rc 2 = catalog unreadable; rc 3 = retired row with a
# malformed absorbed_by (missing kit/range, or range not a semver range).
pack_retire_declaration() {
  local catalog="$1" pid="$2"
  pack_resolve_py3 || return 2
  "$PACK_PY3" - "$catalog" "$pid" "${SEMVER_LIB_PATH:-$(dirname "${BASH_SOURCE[0]}")/semver_range.py}" <<'PYEOF'
import json, subprocess, sys
catalog, pid, semver = sys.argv[1:4]
try:
    with open(catalog, encoding="utf-8") as f:
        rows = json.load(f).get("packs", [])
except Exception as e:
    print("catalog unreadable: %s" % e, file=sys.stderr)
    sys.exit(2)
row = next((r for r in rows if isinstance(r, dict) and r.get("id") == pid), None)
if row is None:
    print("\t\t")
    sys.exit(0)
status = row.get("status") if isinstance(row.get("status"), str) else ""
ab = row.get("absorbed_by")
kit = ab.get("kit") if isinstance(ab, dict) else None
rng = ab.get("range") if isinstance(ab, dict) else None
if status != "retired":
    print("%s\t\t" % status)
    sys.exit(0)
if not isinstance(kit, str) or not kit or not isinstance(rng, str) or not rng:
    print("retired\t\t")
    sys.exit(3)
ok = subprocess.run([sys.executable, semver, "is-range", rng],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
print("retired\t%s\t%s" % (kit, rng))
sys.exit(0 if ok else 3)
PYEOF
}

_pr_conf_packs_ver() { # <conf> <id> -- installed version ('' = none)
  local line it
  line="$(sed -n 's/^DOGANY_PACKS=//p' "$1" 2>/dev/null | head -n1)"
  IFS=',' read -r -a _pr_items <<< "$line"
  for it in ${_pr_items[@]+"${_pr_items[@]}"}; do
    if [[ "${it%%@*}" == "$2" ]]; then printf '%s' "${it#*@}"; return 0; fi
  done
  printf ''
}

# _pr_collect <root> <id> -- the move list, computed ONCE and read by both
# the dry-run plan and the real run (DGN-1422 doctrine: they cannot drift).
# Prints tab-separated rows:
#   FILE\t<rel>            archive (no other owner)
#   KEEP\t<rel>\t<owner>   recorded, but another ledger owns it now
#   GONE\t<rel>            recorded, already absent on disk
#   BAD\t<rel>             unsafe recorded path (refused, never touched)
#   HOOK\t<rel>            a FILE row that is a .py/.sh (hook-unwire candidate)
_pr_collect() {
  local root="$1" pid="$2"
  "$PACK_PY3" - "$root" "$pid" <<'PYEOF'
import glob, os, sys
root, pid = sys.argv[1], sys.argv[2]
cp = os.path.join(root, "config", "packs")

def entries(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    out.append(ln)
    except OSError:
        pass
    return out

own = set(entries(os.path.join(cp, pid + ".files")))
try:
    with open(os.path.join(cp, pid + ".receipt"), encoding="utf-8") as f:
        for ln in f:
            if ln.startswith("anchor="):
                own.add(ln[len("anchor="):].strip())
except OSError:
    pass
own.add("config/%s.conf" % pid)      # the pack's merge-key conf (C4 CONF_OWNER)

others = {}
for p in sorted(glob.glob(os.path.join(cp, "*.files"))):
    oid = os.path.basename(p)[:-len(".files")]
    if oid == pid:
        continue
    for e in entries(p):
        others.setdefault(e, oid)

for rel in sorted(own):
    if not rel:
        continue
    parts = rel.split("/")
    if rel.startswith("/") or ".." in parts or "." in parts or "" in parts:
        print("BAD\t%s" % rel)
        continue
    if rel.startswith(".claude/skills/") or rel.startswith(".claude/skills-bundle/"):
        continue                      # skill surfaces: the .skills lane owns them
    if rel.startswith("config/packs/"):
        continue                      # records: moved wholesale at the end
    if rel in others:
        print("KEEP\t%s\t%s" % (rel, others[rel]))
        continue
    path = os.path.join(root, rel)
    if not os.path.lexists(path):
        print("GONE\t%s" % rel)
        continue
    if os.path.isdir(path) and not os.path.islink(path):
        print("BAD\t%s" % rel)        # ledgers record files; a dir row is not ours to move
        continue
    print("FILE\t%s" % rel)
    if rel.endswith(".py") or rel.endswith(".sh"):
        print("HOOK\t%s" % rel)
PYEOF
}

# _pr_hooks <mode:plan|apply> <root> <archive> <rel...>
# Unwires every hook command "<interp> <root>/<rel>" (the exact shape
# pack_install _hooks_wire writes) from .claude/settings.local.json and the
# framework-owned .claude/settings.json (pre-DGN-1250 wiring lived there).
# plan: prints what would go; apply: rewrites atomically, saves removed
# entries to <archive>/hooks-removed.json. A file that does not parse is
# reported and left alone (never rewritten blind).
_pr_hooks() {
  local mode="$1" root="$2" archive="$3"
  shift 3
  "$PACK_PY3" - "$mode" "$root" "$archive" "$@" <<'PYEOF'
import json, os, sys, tempfile
mode, root, archive = sys.argv[1:4]
rels = sys.argv[4:]
cmds = set()
for rel in rels:
    path = os.path.join(root, rel)
    for interp in ("/usr/bin/python3", "/bin/bash"):
        cmds.add("%s %s" % (interp, path))
removed = {}
for name in ("settings.local.json", "settings.json"):
    f = os.path.join(root, ".claude", name)
    if not os.path.isfile(f):
        continue
    try:
        with open(f, encoding="utf-8") as fh:
            s = json.load(fh)
    except ValueError as e:
        print("WARN\t%s does not parse (%s) -- hook entries there left untouched" % (f, e))
        continue
    hooks = s.get("hooks") if isinstance(s, dict) else None
    if not isinstance(hooks, dict):
        continue
    gone = []
    for ev in list(hooks):
        lst = hooks[ev]
        if not isinstance(lst, list):
            continue
        keep = []
        for entry in lst:
            if not isinstance(entry, dict):
                keep.append(entry)
                continue
            if entry.get("command") in cmds:
                gone.append([ev, entry])
                continue
            inner = entry.get("hooks")
            if isinstance(inner, list):
                kept = [h for h in inner if not (isinstance(h, dict) and h.get("command") in cmds)]
                if len(kept) != len(inner):
                    gone.append([ev, {"hooks": [h for h in inner if h not in kept]}])
                    if not kept:
                        continue
                    entry = dict(entry, hooks=kept)
            keep.append(entry)
        if keep:
            hooks[ev] = keep
        else:
            del hooks[ev]
    if not gone:
        continue
    for ev, entry in gone:
        for h in (entry.get("hooks") or [entry]):
            print("HOOK\t%s\t%s\t%s" % (name, ev, h.get("command")))
    removed[name] = gone
    if mode == "apply":
        fd, tmp = tempfile.mkstemp(prefix="." + name + ".tmp.", dir=os.path.dirname(f))
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(s, out, indent=2)
            out.write("\n")
        os.chmod(tmp, os.stat(f).st_mode & 0o7777)
        os.replace(tmp, f)
if mode == "apply" and removed:
    os.makedirs(archive, exist_ok=True)
    with open(os.path.join(archive, "hooks-removed.json"), "w", encoding="utf-8") as out:
        json.dump(removed, out, indent=2)
        out.write("\n")
PYEOF
}

# _pr_conf_drop <conf> <id> -- drop <id> from DOGANY_PACKS and
# DOGANY_PACK_PINS, atomically (flock <conf>.lock + tmp + fsync + replace).
_pr_conf_drop() {
  "$PACK_PY3" - "$1" "$2" <<'PYEOF'
import fcntl, os, sys, tempfile, time, pathlib
conf, pid = pathlib.Path(sys.argv[1]), sys.argv[2]
lock_fh = open(str(conf) + ".lock", "a")
deadline = time.monotonic() + float(os.environ.get("DOGANY_CONF_LOCK_TIMEOUT", "30"))
while True:
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except OSError:
        if time.monotonic() >= deadline:
            sys.stderr.write("[pack-retire] FATAL: conf lock timeout -- conf untouched\n")
            sys.exit(1)
        time.sleep(0.05)
try:
    lines = conf.read_text(encoding="utf-8").splitlines()
    out, changed = [], False
    for ln in lines:
        for key, sep in (("DOGANY_PACKS=", "@"), ("DOGANY_PACK_PINS=", "=")):
            if ln.startswith(key):
                items = [x for x in ln[len(key):].split(",") if x]
                kept = [x for x in items if x.split(sep, 1)[0] != pid]
                if kept != items:
                    changed = True
                    ln = key + ",".join(kept)
        out.append(ln)
    if changed:
        mode = conf.stat().st_mode & 0o7777
        fd, tmp = tempfile.mkstemp(prefix=".instance.conf.tmp.", dir=str(conf.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, conf)
    print("changed" if changed else "unchanged")
finally:
    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    lock_fh.close()
PYEOF
}

# pack_retire_run <root> <id> <catalog> <dry:0|1>
# Logs through the caller's _log; refusals through _fail (pack_install.sh).
# rc 0 = retired (or nothing left to retire); refusals exit via _fail.
pack_retire_run() {
  local root="$1" pid="$2" catalog="$3" dry="$4"
  local conf="$root/.instance.conf" decl drc st kit rng inst kver ev ev_v
  local rows kind rel owner ts archive n_file=0 n_keep=0 n_skill=0 n_skill_keep=0
  local hook_rels=() f name

  [[ -f "$conf" ]] || _fail "retire $pid: $conf not found -- not an instance root"
  drc=0
  decl="$(pack_retire_declaration "$catalog" "$pid")" || drc=$?
  st="$(printf '%s' "$decl" | cut -f1)"
  kit="$(printf '%s' "$decl" | cut -f2)"
  rng="$(printf '%s' "$decl" | cut -f3)"
  case "$drc" in
    0) : ;;
    3) _fail "retire $pid: catalog row is status=retired but absorbed_by is malformed (needs {\"kit\": <id>, \"range\": <semver range>}) -- refusing to act on a half declaration ($catalog)" ;;
    *) _fail "retire $pid: catalog unreadable ($catalog) -- retirement declaration unknown, fail-closed" ;;
  esac
  [[ "$st" == "retired" ]] \
    || _fail "retire $pid: catalog row status='${st:-absent}' -- only a pack whose catalog row declares status=retired + absorbed_by can be retired (DGN-1922; declaration-driven, never ad hoc)"

  inst="$(_pr_conf_packs_ver "$conf" "$pid")"
  if [[ -z "$inst" ]] && ! compgen -G "$root/config/packs/$pid.*" >/dev/null; then
    _log "retire $pid: nothing to retire -- no DOGANY_PACKS entry and no config/packs/$pid.* record (already retired or never installed)"
    return 0
  fi

  # Gate 0: the absorbing kit is installed inside the declared range.
  kver="$(_pr_conf_packs_ver "$conf" "$kit")"
  [[ -n "$kver" ]] \
    || _fail "retire $pid: absorbing kit '$kit' is not installed on this instance (no DOGANY_PACKS $kit@ entry) -- install/upgrade $kit $rng first, then retire (DGN-1922)"
  if ! "$PACK_PY3" "$(dirname "${BASH_SOURCE[0]}")/semver_range.py" satisfies "$kver" "$rng" >/dev/null 2>&1; then
    _fail "retire $pid: absorbing kit $kit@$kver does not satisfy absorbed_by.range '$rng' -- the absorbing module is not there yet; retiring now would remove a live feature (DGN-1922)"
  fi
  ev="$(evidence_consume "$root" "$kit" "$kver")"
  ev_v="${ev%%|*}"
  case "$ev_v" in
    VERIFIED|UNKNOWN) _log "retire $pid: absorbing kit $kit@$kver satisfies '$rng' (install evidence $ev_v)" ;;
    *) _fail "retire $pid: absorbing kit $kit@$kver install evidence $ev_v (${ev#*|}) -- the ledger alone is not proof the absorbing module landed (DGN-1456 fail-closed)" ;;
  esac

  ts="$(date +%Y%m%d-%H%M%S)"
  archive="$root/.claude/packs-retired/$pid.$ts"
  rows="$(_pr_collect "$root" "$pid")"
  if [[ "$dry" -eq 1 ]]; then
    _log "retire $pid: PLAN (dry-run, zero writes) -- installed ${inst:-<no ledger entry>}, absorbed by $kit $rng; archive would be $archive"
  else
    _log "retire $pid: RETIRE -- installed ${inst:-<no ledger entry>}, absorbed by $kit $rng; archive $archive"
  fi

  # 1) skills
  if [[ -f "$root/config/packs/$pid.skills" ]]; then
    while IFS= read -r name; do
      [[ -n "$name" && "$name" != \#* ]] || continue
      if owner="$(skill_retire_other_owner "$root" "$name" "$pid")"; then
        n_skill_keep=$((n_skill_keep + 1))
        _log "  skill $name: kept -- still declared by '$owner' (now its asset)"
        continue
      fi
      n_skill=$((n_skill + 1))
      if [[ "$dry" -eq 1 ]]; then
        while IFS= read -r f; do _log "  $f"; done < <(skill_retire_plan "$root" "" "$name")
      else
        while IFS= read -r f; do _log "  $f"; done < <(skill_retire_apply "$root" "" "$name" "$ts")
      fi
    done < "$root/config/packs/$pid.skills"
  fi

  # 2) files
  while IFS=$'\t' read -r kind rel owner; do
    case "$kind" in
      KEEP) n_keep=$((n_keep + 1)); _log "  file $rel: kept -- also recorded by '$owner'" ;;
      GONE) _log "  file $rel: already absent" ;;
      BAD)  _log "  file $rel: WARN unsafe or non-file ledger row -- never touched" ;;
      HOOK) hook_rels+=("$rel") ;;
      FILE)
        n_file=$((n_file + 1))
        if [[ "$dry" -eq 1 ]]; then
          _log "  file $rel: would archive -> $archive/files/$rel"
        elif mkdir -p "$(dirname "$archive/files/$rel")" && mv "$root/$rel" "$archive/files/$rel"; then
          _log "  file $rel: archived"
          # prune now-empty dirs of the pack's own service namespace only
          # (service/<ns>/...); shared surfaces like routines/bundle/ stay.
          f="$(dirname "$rel")"
          while [[ "$f" == service/* ]] && rmdir "$root/$f" 2>/dev/null; do f="$(dirname "$f")"; done
        else
          _fail "retire $pid: could not archive $rel -- stopping before the ledger/records change (re-run resumes)"
        fi ;;
    esac
  done <<< "$rows"

  # 3) hooks
  if [[ "${#hook_rels[@]}" -gt 0 ]]; then
    while IFS=$'\t' read -r kind f owner rel; do
      case "$kind" in
        HOOK) _log "  hook $owner ($f): $([[ "$dry" -eq 1 ]] && echo "would unwire" || echo "unwired") $rel" ;;
        WARN) _log "  hook: WARN $f" ;;
      esac
    done < <(_pr_hooks "$([[ "$dry" -eq 1 ]] && echo plan || echo apply)" "$root" "$archive" "${hook_rels[@]}")
  fi

  if [[ "$dry" -eq 1 ]]; then
    if [[ -n "$inst" ]]; then
      _log "  ledger: would drop $pid@$inst from DOGANY_PACKS (and any DOGANY_PACK_PINS entry)"
    fi
    for f in "$root/config/packs/$pid".*; do
      if [[ -e "$f" ]]; then _log "  record: would archive config/packs/$(basename "$f")"; fi
    done
    _log "retire $pid: PLAN -- skills archive $n_skill / kept $n_skill_keep, files archive $n_file / kept $n_keep"
    return 0
  fi

  # 4) ledger
  _log "  ledger: DOGANY_PACKS $(_pr_conf_drop "$conf" "$pid") (dropped $pid)"

  # 5) records
  for f in "$root/config/packs/$pid".*; do
    [[ -e "$f" ]] || continue
    mkdir -p "$archive/config-packs"
    mv "$f" "$archive/config-packs/" || _fail "retire $pid: could not archive record $f"
    _log "  record: archived config/packs/$(basename "$f")"
  done

  # 6) receipt
  mkdir -p "$archive"
  {
    echo "schema=1"
    echo "pack=$pid"
    echo "version=${inst:-}"
    echo "absorbed_by=$kit@$kver"
    echo "range=$rng"
    echo "retired_at=$ts"
    echo "archive=${archive#"$root"/}"
    echo "skills_archived=$n_skill"
    echo "skills_kept=$n_skill_keep"
    echo "files_archived=$n_file"
    echo "files_kept=$n_keep"
  } > "$archive/RETIRE-RECEIPT"
  cp -f "$archive/RETIRE-RECEIPT" "$root/.claude/packs-retired/$pid.retired"
  _log "retire $pid: DONE -- receipt .claude/packs-retired/$pid.retired (skills archived $n_skill / kept $n_skill_keep, files archived $n_file / kept $n_keep)"
  return 0
}

# Direct-execution guard: read-only declaration probe (tests).
#   pack_retire.sh declaration <catalog> <id>
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/py_resolve.sh"
  if [[ "${1:-}" != "declaration" || $# -ne 3 ]]; then
    echo "usage: pack_retire.sh declaration <catalog> <id>" >&2
    exit 1
  fi
  pack_retire_declaration "$2" "$3"
fi
