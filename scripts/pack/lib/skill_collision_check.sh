#!/bin/bash
# skill_collision_check.sh -- cross-pack skill collision verdict (DGN-1143).
# Sourced function library (`skill_collision_check "$@"`), with a direct-
# execution guard so the test harness can still run it standalone. Requires
# python3.
#
# THE PREDICATE (DGN-1143 §설계): if the set of packs installable onto one
# instance ships the same skill name from two or more packs, the verdict is
# FAIL -- unless EVERY shipping pack's manifest declares the SAME winner for
# that name (skills[] entry key "winner", value = a shipping pack's id).
# Install order must never be what decides which copy of a skill an instance
# ends up with (the disease: diet-log/workout-log shipped by lifekit AND
# health-trainer, three-way diverged -- measured on a live instance
# 2026-08-27, DGN-1143 관찰 1).
#
# Winner declaration (additive -- contract_version stays 1, DGN-956 seam):
#   "skills": [ {"name": "diet-log", "sharing_mode": "share",
#                "winner": "lifekit"} ]
# The "winner" key is OPTIONAL and only meaningful while a collision exists;
# under --strict-declarations a winner declared for a NON-colliding name is a
# FAIL (dead declaration, C7 precedent -- declarations must not rot).
#
# THE FRAMEWORK IS A SHIPPER TOO (DGN-1143 M4). Until M4 the universe was
# "catalog rows" only, and packs/catalog.json has no framework row -- so the
# framework's own delivery surfaces (skills/, agents/.template/.claude/skills*)
# could never be judged, no matter how the gate was tuned. The framework now
# joins the universe as a synthetic member id "framework", and its shipped set
# is DERIVED FROM THE TWO DELIVERERS, never from a hand-maintained list (see
# "REGISTRY DERIVATION" in the python below). Registration is a side effect of
# being wired into a deliverer -- forgetting to declare a new surface cannot
# hide it, because nothing is declared in the first place (DGN-1012 R1 logic:
# bind the guarantee to the launcher, not to the surface).
#
# SCOPE: pairs involving ONE pack under judgment (--self). Coverage of the
# whole universe is inductive: every pack passes through this gate at ITS
# publish/install, so a new collision cannot enter without one of its two
# shippers being the --self of some run. (A full-pairs sweep was rejected:
# it would let an unrelated dirty pair block this pack's publish, and the
# publisher may legitimately lack checkouts of packs it does not touch.)
#
# Universe resolution (fail-closed -- 판정 불능 is a FAIL, never a SKIP):
#   - default: every catalog row with status=="published", id != self;
#   - --restrict <id,id,...>: exactly those ids (install-side: the packs
#     RECORDED as installed on the target instance). A restricted id with no
#     catalog row = FAIL (stale record -- e.g. a pack renamed/removed by
#     dec-145 without migrating instance records; loud, never silent).
#   - a universe member whose package_dir does not exist on disk, or whose
#     pack-manifest.json is missing/unparseable = FAIL (cannot certify
#     no-collision against a pack it cannot read).
# Shipped skills = child dirs of <package_dir>/<payload_root>/skills-bundle/
# (payload_root defaults to "payload"; a missing skills-bundle dir is a valid
# observation of 0 skills, NOT a resolution failure).
#
# Verdict output (DGN-1142 §3.1 -- counts on EVERY verdict; the publish.sh
# gate E3 pattern: 0 findings and not-run must never share a sentence):
#   PASS  "... skills checked: N, collisions: M (winner-resolved: R)"
#   n/a   self ships no skills, or the universe has no other pack -- these
#         are "대상 없음", worded distinctly from "checked and clean"
#   FAIL  per-collision detail lines + a summary carrying the same counts
# Exit codes: 0 = PASS or n/a, 1 = FAIL (collision without agreed winner,
# dead winner declaration under --strict-declarations, or 판정 불능).
#
# Usage (sourced -- the E2b-visible static form; see below). Dot-source this
# file via a VAR="$SCRIPT_DIR/lib/skill_collision_check.sh" assignment (the
# literal invocation is not spelled here: E2 pass B greps comments too, and
# a spelled-out example would register as a phantom unresolved ref), then:
#   skill_collision_check --self <id>=<dir> --catalog <catalog.json>
#                         [--restrict <id1,id2,...>]
#                         [--strict-declarations]
# Registry mode (the framework's OWN delivery surfaces, no pack under
# judgment -- prints the derived registry, FAILs on husk/divergence):
#   skill_collision_check --framework-registry <repo-root>
# Or standalone (test harness): bash skill_collision_check.sh <same args>
#
# Call sites: pack_publish.sh GATE (e) (publish-side, full published-row
# universe, strict) and pack_install.sh inline gate (install-side, universe
# restricted to the instance's config/packs/*.requires_framework records).
# WHY SOURCED, not `bash "$LIB"`: scripts/pack/lib/** ships via the
# product.yaml glob and publish.sh gate E2b requires every file here to be
# statically referenced by a KEPT script. The reference scanner (E2 pass B)
# recognizes `source`/`.`/`python3` invocations -- NOT `bash "$VAR"` -- so
# the first DGN-1143 shape (standalone script, bash-invoked) was measured
# unreferenced and BLOCKED the publish dry-run (EXIT=1, 2026-08-27). Both
# consumers now `.`-source this file, the exact idiom every other lib in
# this directory already uses; do not regress to a bash-subprocess call.

skill_collision_check() {
  local SELF_SPEC="" CATALOG="" RESTRICT="" STRICT=0 FW_REGISTRY=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --self)                SELF_SPEC="$2"; shift 2 ;;
      --catalog)             CATALOG="$2";   shift 2 ;;
      --restrict)            RESTRICT="$2";  shift 2 ;;
      --strict-declarations) STRICT=1;       shift ;;
      --framework-registry)  FW_REGISTRY="$2"; shift 2 ;;
      *) echo "[skill-collision] FAIL: unknown argument '$1'" >&2; return 1 ;;
    esac
  done

  if [[ -z "$FW_REGISTRY" ]]; then
    [[ -n "$SELF_SPEC" && "$SELF_SPEC" == *=* ]] \
      || { echo "[skill-collision] FAIL: --self <id>=<dir> required" >&2; return 1; }
    [[ -n "$CATALOG" ]] \
      || { echo "[skill-collision] FAIL: --catalog required" >&2; return 1; }
    [[ -f "$CATALOG" ]] \
      || { echo "[skill-collision] FAIL: catalog not found: $CATALOG -- universe unknown, refusing to certify no-collision (fail-closed)" >&2; return 1; }
  else
    [[ -d "$FW_REGISTRY" ]] \
      || { echo "[skill-collision] FAIL: --framework-registry root not a directory: $FW_REGISTRY (fail-closed)" >&2; return 1; }
  fi
  command -v python3 >/dev/null 2>&1 \
    || { echo "[skill-collision] FAIL: python3 unavailable -- verdict impossible (fail-closed)" >&2; return 1; }

  python3 - "$SELF_SPEC" "$CATALOG" "$RESTRICT" "$STRICT" "$FW_REGISTRY" <<'PYEOF'
import fnmatch, json, os, re, sys

self_spec, catalog_path, restrict_raw, strict_raw, fw_registry_root = sys.argv[1:6]
strict = strict_raw == "1"
self_id, _, self_dir = self_spec.partition("=")

FAILS = []
def fail(msg):
    FAILS.append(msg)

def load_manifest(pack_id, pdir):
    mf = os.path.join(pdir, "pack-manifest.json")
    if not os.path.isdir(pdir):
        fail("pack '%s': package dir not on disk: %s -- cannot read its shipped "
             "skills, refusing to certify no-collision (fail-closed)" % (pack_id, pdir))
        return None
    try:
        with open(mf) as f:
            d = json.load(f)
    except Exception as e:
        fail("pack '%s': pack-manifest.json unreadable (%s: %s) -- fail-closed"
             % (pack_id, mf, e))
        return None
    if not isinstance(d, dict):
        fail("pack '%s': pack-manifest.json is not a JSON object -- fail-closed" % pack_id)
        return None
    return d

def shipped_skills(pack_id, pdir, manifest):
    root = manifest.get("payload_root")
    root = root if isinstance(root, str) and root else "payload"
    bundle = os.path.join(pdir, root, "skills-bundle")
    if not os.path.isdir(bundle):
        return []          # valid observation: this pack ships no skills
    return sorted(e for e in os.listdir(bundle)
                  if not e.startswith(".") and os.path.isdir(os.path.join(bundle, e)))

def winner_map(manifest):
    out = {}
    for e in manifest.get("skills") or []:
        if isinstance(e, dict) and isinstance(e.get("name"), str):
            w = e.get("winner")
            if isinstance(w, str) and w:
                out[e["name"]] = w
    return out

# ---------------------------------------------------------------------------
# REGISTRY DERIVATION -- the framework's own delivery surfaces.
#
# 판정 순서: 존재 != 배선됨 != 도달함. A directory full of SKILL.md files is
# not a delivery surface; a surface a DELIVERER copies onto an instance is.
# So the registry is read off the two deliverers, not off the disk layout and
# not off a list somebody has to remember to update:
#
#   deliverer 1  update.sh          SKILLS_ROOT="$REPO_ROOT/skills"        (:97)
#                                   for d in "$SKILLS_ROOT"/dogany-*/      (:3970)
#                                   dest="$INSTANCE/.claude/skills/$name"  (:3974)
#                                   rsync -aL --delete "$d" "$dest/"       (:4016)
#                => surface (skills/, glob dogany-*)   -- ONE surface, and the
#                   glob is read from the loop, so a widened glob widens the
#                   registry with no edit here.
#
#   deliverer 2  scripts/mint.sh    TEMPLATE="$REPO_ROOT/agents/.template" (:38)
#                                   rsync -aL ... (:253)
#                                     "$TEMPLATE/" "$PROJECT_ROOT/"        (:275)
#                => mint copies the WHOLE template, dereferencing symlinks.
#                   Therefore EVERY skills* directory under <template>/.claude
#                   reaches the instance, and the registry DISCOVERS them by
#                   listing that directory. Adding
#                   .template/.claude/skills-whatever/ registers it; there is
#                   nothing to forget to declare.
#
# ARMED CHECK (DGN-1012 §"R1 만으로는 부족하다"): every anchor above is
# asserted present. If a deliverer is rewritten and an anchor moves, the
# derivation's premise is void and this FAILS -- it does not quietly return a
# smaller registry. "0을 봤다" and "볼 수 없었다" must not share an output.
#
# REALPATH FOLDING (DGN-1143 M4 §3): 9 of the 10 entries under
# .template/.claude/skills are SYMLINKS to ../../../../skills/<name> -- the
# same delivery written twice. Folding by name would swallow the one real
# divergence along with them (M2 folded sources and went 6->5). Folding by
# os.path.realpath keeps aliases collapsed AND keeps a second REAL copy
# visible: measured 2026-08-28, name-level duplicates 10 -> realpath-level
# divergences 1.
# ---------------------------------------------------------------------------

FW_ID = "framework"

def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None

def framework_root_from_catalog(cp):
    """<root>/packs/catalog.json -> <root>; anything else -> None (a synthetic
    test catalog, not a framework tree)."""
    d = os.path.dirname(os.path.abspath(cp))
    if os.path.basename(d) != "packs":
        return None
    return os.path.dirname(d)

def framework_surfaces(root):
    """[(label, abs_dir, glob)] derived from the deliverer chokepoints.
    Appends to FAILS and returns [] when an anchor is missing."""
    out = []

    up_path = os.path.join(root, "update.sh")
    up = _read_text(up_path)
    if up is None:
        fail("framework deliverer unreadable: %s -- delivery-surface registry "
             "cannot be derived (fail-closed)" % up_path)
        return []
    m_root = re.search(r'(?m)^SKILLS_ROOT="\$REPO_ROOT/([^"]+)"\s*$', up)
    m_glob = re.search(r'(?m)^for d in "\$SKILLS_ROOT"/([^/"]+)/; do\s*$', up)
    if not m_root or not m_glob or 'dest="$INSTANCE/.claude/skills/$name"' not in up:
        fail("update.sh delivery anchors not found (SKILLS_ROOT= / "
             'for d in "$SKILLS_ROOT"/<glob>/ / dest=".../.claude/skills/$name") '
             "-- the deliverer was rewritten and this registry's premise is void "
             "(fail-closed; a smaller registry would be a silent hole)")
    else:
        d = os.path.join(root, m_root.group(1))
        if os.path.isdir(d):
            out.append((m_root.group(1), d, m_glob.group(1)))
        else:
            fail("update.sh names SKILLS_ROOT=%s but that directory does not "
                 "exist under %s (fail-closed)" % (m_root.group(1), root))

    mint_path = os.path.join(root, "scripts", "mint.sh")
    mt = _read_text(mint_path)
    if mt is None:
        fail("framework deliverer unreadable: %s -- delivery-surface registry "
             "cannot be derived (fail-closed)" % mint_path)
        return out
    m_tmpl = re.search(r'(?m)^TEMPLATE="\$REPO_ROOT/([^"]+)"\s*$', mt)
    whole_tree_copy = '"$TEMPLATE/" "$PROJECT_ROOT/"' in mt and "rsync -aL" in mt
    if not m_tmpl or not whole_tree_copy:
        fail("mint.sh delivery anchors not found (TEMPLATE= / "
             'rsync -aL ... "$TEMPLATE/" "$PROJECT_ROOT/") -- the whole-template '
             "copy is what makes every template skills* dir a delivery surface; "
             "without it the registry cannot be derived (fail-closed)")
        return out
    tdir = os.path.join(root, m_tmpl.group(1), ".claude")
    if not os.path.isdir(tdir):
        fail("mint.sh names TEMPLATE=%s but %s does not exist (fail-closed)"
             % (m_tmpl.group(1), tdir))
        return out
    for e in sorted(os.listdir(tdir)):
        if not fnmatch.fnmatch(e, "skills*"):
            continue
        d = os.path.join(tdir, e)
        if os.path.isdir(d):
            out.append((os.path.join(m_tmpl.group(1), ".claude", e), d, "*"))
    return out

def framework_registry(root):
    """(surfaces, entries, index, husks, disp) where
       entries = [(label, folded-name, realpath)] every delivered child dir,
       index   = {folded-name: {realpath: [label,...]}} after realpath folding,
       disp    = {folded-name: display name first seen}.
    Names are CASE-FOLDED for the same reason the cross-pack key is
    (instances land on APFS/HFS+ default case-insensitive filesystems, where
    .claude/skills/Dogany-X and .claude/skills/dogany-x are ONE directory --
    exact-string comparison would wave the divergence through while the disk
    merges it).
    A husk (child dir with no SKILL.md) is a payload defect, not a skill:
    every enumerator in this estate counts child DIRS, so a husk reads as a
    shipped skill and manufactures a phantom collision (DGN-1143 M2 §1.2,
    measured: one __pycache__-only dir -> 'collisions: 1', EXIT=1). It is
    reported as a FAIL, never silently dropped -- dropping it would be the
    gate going soft on a real payload defect."""
    surfaces = framework_surfaces(root)
    entries, index, husks, disp = [], {}, [], {}
    for label, d, glob in surfaces:
        for e in sorted(os.listdir(d)):
            if e.startswith(".") or not fnmatch.fnmatch(e, glob):
                continue
            pth = os.path.join(d, e)
            if not os.path.isdir(pth):        # follows symlinks, by design
                continue
            if not os.path.isfile(os.path.join(pth, "SKILL.md")):
                husks.append("%s/%s" % (label, e))
                continue
            k = e.lower()
            disp.setdefault(k, e)
            rp = os.path.realpath(pth)
            entries.append((label, k, rp))
            index.setdefault(k, {}).setdefault(rp, []).append(label)
    return surfaces, entries, index, husks, disp

def framework_verdict(root):
    """Resolve the framework as a universe member. Returns
    (skills sorted, summary line). Records FAILs for husks and for
    realpath-level divergence (the same name delivered from two REAL
    copies -- install order decides the winner, which is this ticket's
    whole disease, here inside the framework itself)."""
    surfaces, entries, index, husks, disp = framework_registry(root)
    # relpath against the RESOLVED root: on macOS /tmp is a symlink to
    # /private/tmp, so relpath(realpath(x), "/tmp/...") emits a ../../.. path
    # that reads like a traversal bug in the failure message.
    rroot = os.path.realpath(root)
    for h in sorted(husks):
        fail("framework delivery surface carries a husk directory '%s' (no "
             "SKILL.md) -- every surface enumerator counts child dirs, so a "
             "husk ships as a phantom skill. Delete the leftover, or restore "
             "the skill" % h)
    multi = sorted(n for n, rps in index.items() if len(rps) > 1)
    for n in multi:
        where = "; ".join(
            "%s (via %s)" % (os.path.relpath(rp, rroot), ", ".join(sorted(labels)))
            for rp, labels in sorted(index[n].items()))
        fail("framework ships '%s' from %d DISTINCT real copies -- install "
             "order decides which one an instance keeps (mint lands the "
             "template copy, the first update.sh overwrites it with the "
             "skills/ copy, no backup and no warning). Fold one into the "
             "other (symlink) or delete the loser: %s"
             % (disp[n], len(index[n]), where))
    names = sorted(disp[k] for k in index)
    aliased = sum(1 for n in index
                  if len(index[n]) == 1
                  and len([1 for lbl, nm, rp in entries if nm == n]) > 1)
    summary = ("framework surfaces: %d [%s], entries: %d, names: %d "
               "(alias-folded: %d, divergent: %d), husks: %d"
               % (len(surfaces), " ".join(l for l, _, _ in surfaces),
                  len(entries), len(names), aliased, len(multi), len(husks)))
    return names, summary

# ---- framework registry mode (no pack under judgment) ----
if fw_registry_root:
    fw_names, fw_summary = framework_verdict(os.path.abspath(fw_registry_root))
    if FAILS:
        for m in FAILS:
            print("[skill-collision] FAIL: %s" % m, file=sys.stderr)
        print("[skill-collision] FAIL: framework delivery registry -- %s"
              % fw_summary, file=sys.stderr)
        sys.exit(1)
    for n in fw_names:
        print("[skill-collision]   framework ships: %s" % n)
    print("[skill-collision] PASS: framework delivery registry -- %s" % fw_summary)
    sys.exit(0)

# ---- framework as a universe member (automatic -- nothing is declared) ----
# The framework root is DERIVED from where the catalog sits: packs/catalog.json
# lives at <framework-root>/packs/. No flag, no call-site edit, no catalog row:
# both existing consumers (pack_publish GATE (e), pack_install inline gate) get
# the framework member the moment they pass the real catalog. A synthetic test
# catalog resolves to no framework tree -- and that state is PRINTED, because
# "framework not checked" and "framework checked, clean" must never share a
# sentence (DGN-1142 §3.1).
fw_root = framework_root_from_catalog(catalog_path) if catalog_path else None
fw_up   = os.path.isfile(os.path.join(fw_root, "update.sh")) if fw_root else False
fw_mint = os.path.isfile(os.path.join(fw_root, "scripts", "mint.sh")) if fw_root else False
fw_skills, fw_present = [], False
if fw_root and fw_up and fw_mint:
    fw_present = True
    fw_skills, fw_summary = framework_verdict(fw_root)
    fw_note = "framework: IN universe -- %s" % fw_summary
elif fw_root and (fw_up or fw_mint):
    fail("framework tree at %s has only one of its two deliverers (update.sh=%s, "
         "scripts/mint.sh=%s) -- half a tree cannot be certified (fail-closed)"
         % (fw_root, fw_up, fw_mint))
    fw_note = "framework: UNRESOLVABLE"
else:
    fw_note = ("framework: NOT in universe -- catalog %s is not "
               "<framework-root>/packs/catalog.json with both deliverers present, "
               "so the framework's own delivery surfaces were NOT checked in this "
               "run (not-run, not a clean sweep)" % catalog_path)

# Framework health is a PRECONDITION of every verdict this gate issues: a husk
# or a two-real-copies split on a framework surface makes any "no collision"
# claim unsound, so it blocks before the pack under judgment is even read.
if FAILS:
    for m in FAILS:
        print("[skill-collision] FAIL: %s" % m, file=sys.stderr)
    print("[skill-collision] FAIL: framework delivery surfaces unfit -- refusing "
          "to certify any pack against them (fail-closed)", file=sys.stderr)
    sys.exit(1)
print("[skill-collision] %s" % fw_note)

# ---- self ----
self_mf = load_manifest(self_id, self_dir)
if self_mf is None:
    for m in FAILS: print("[skill-collision] FAIL: %s" % m, file=sys.stderr)
    sys.exit(1)
self_skills = shipped_skills(self_id, self_dir, self_mf)
self_winners = winner_map(self_mf)

if not self_skills:
    print("[skill-collision] n/a: pack '%s' ships no skills (skills checked: 0, "
          "collisions: 0) -- nothing to collide; this is 대상 없음, not a clean sweep"
          % self_id)
    sys.exit(0)

# ---- universe ----
try:
    with open(catalog_path) as f:
        cat = json.load(f)
    rows = {r["id"]: r for r in cat.get("packs", []) if isinstance(r, dict) and "id" in r}
except Exception as e:
    print("[skill-collision] FAIL: catalog unparseable (%s: %s) -- universe unknown, "
          "fail-closed" % (catalog_path, e), file=sys.stderr)
    sys.exit(1)

catalog_dir = os.path.dirname(os.path.abspath(catalog_path))
def resolve_dir(row):
    pd = row.get("package_dir") or row["id"]
    return pd if os.path.isabs(pd) else os.path.join(catalog_dir, pd)

others = []   # (id, dir)
if restrict_raw:
    for rid in [x for x in restrict_raw.split(",") if x]:
        if rid == self_id:
            continue
        row = rows.get(rid)
        if row is None:
            fail("restricted universe id '%s' has NO catalog row -- stale install "
                 "record (renamed/removed pack? migrate config/packs/ records), "
                 "fail-closed" % rid)
            continue
        others.append((rid, resolve_dir(row)))
else:
    for rid, row in sorted(rows.items()):
        if rid == self_id or row.get("status") != "published":
            continue
        others.append((rid, resolve_dir(row)))

# Collision key is the CASE-FOLDED name: instances land skills on APFS/HFS+
# default case-INSENSITIVE filesystems, where skills-bundle/Diet-Log and
# skills-bundle/diet-log are one directory -- exact-string comparison would
# wave the collision through while the disk merges it (그릴 발견, DGN-1143).
ship = {}   # folded skill name -> {pack_id: winner-or-None}
disp = {}   # folded name -> display name first seen
for s in self_skills:
    k = s.lower()
    disp.setdefault(k, s)
    ship.setdefault(k, {})[self_id] = self_winners.get(s)

for rid, rdir in others:
    mf = load_manifest(rid, rdir)
    if mf is None:
        continue
    skills = shipped_skills(rid, rdir, mf)
    wmap = winner_map(mf)
    for s in skills:
        k = s.lower()
        if k in ship:
            disp.setdefault(k, s)
            ship[k][rid] = wmap.get(s)

# The framework declares no winners (it has no pack-manifest.json), so any
# framework/pack overlap is UNRESOLVABLE by construction -- fail-closed and
# deliberate: a pack must not be able to elect itself the owner of a name the
# framework already delivers to every instance. Renaming the pack's skill is
# the remedy. (2026-08-28: measured overlap is 0 -- framework ships dogany-*
# plus the two bundle members, packs ship neither.)
for s in fw_skills:
    k = s.lower()
    if k in ship:
        disp.setdefault(k, s)
        ship[k][FW_ID] = None

if FAILS:
    for m in FAILS: print("[skill-collision] FAIL: %s" % m, file=sys.stderr)
    print("[skill-collision] FAIL: universe unresolvable -- skills checked: %d, "
          "collisions: UNKNOWN (판정 불능 = FAIL, never silent)"
          % len(self_skills), file=sys.stderr)
    sys.exit(1)

if not others and not fw_present:
    print("[skill-collision] n/a: no other pack in the universe (self '%s' ships "
          "%d skill(s); skills checked: %d, collisions: 0 by absence of a "
          "counterpart, not by verification)" % (self_id, len(self_skills), len(self_skills)))
    sys.exit(0)

collisions = {k: p for k, p in ship.items() if len(p) > 1}
resolved, unresolved = [], []
for k, shippers in sorted(collisions.items()):
    declared = set(shippers.values())
    if len(declared) == 1 and None not in declared and declared.pop() in shippers:
        resolved.append(k)
    else:
        desc = ", ".join("%s->%s" % (pid, w if w else "(no winner declared)")
                         for pid, w in sorted(shippers.items()))
        unresolved.append((k, desc))

dead = []
if strict:
    for s, w in sorted(self_winners.items()):
        if s.lower() not in collisions:
            dead.append((s, w))

for k, desc in unresolved:
    print("[skill-collision] FAIL: skill '%s' shipped by %d packs without an agreed "
          "winner declaration [%s] -- install order would decide the winner"
          % (disp[k], len(collisions[k]), desc), file=sys.stderr)
for s, w in dead:
    print("[skill-collision] FAIL: dead winner declaration -- '%s' declares "
          "winner='%s' for skill '%s' but no pack in the universe collides on it "
          "(remove the stale declaration; C7 dead-declaration precedent)"
          % (self_id, w, s), file=sys.stderr)

n = len(self_skills)
m = len(collisions)
r = len(resolved)
counts = ("packs in universe: %d (+self%s), skills checked: %d, collisions: %d "
          "(winner-resolved: %d, unresolved: %d)"
          % (len(others), " +framework" if fw_present else "",
             n, m, r, len(unresolved)))
if unresolved or dead:
    print("[skill-collision] FAIL: cross-pack skill collision -- %s%s"
          % (counts, ", dead winner declarations: %d" % len(dead) if dead else ""),
          file=sys.stderr)
    sys.exit(1)
print("[skill-collision] PASS: cross-pack skill collision -- %s" % counts)
sys.exit(0)
PYEOF
}

# Direct-execution guard: the test harness (test-pack-skill-collision.sh)
# runs this file standalone via `bash "$CHECKER" <args>`; runtime consumers
# source it and call the function instead.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  set -euo pipefail
  skill_collision_check "$@"
fi
