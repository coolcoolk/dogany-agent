# pack_commit_gate.sh -- D1 predicate: "is the pack_version this install
# carries reproducible from git?"  Sourced library (no shebang, no side
# effects). Requires python3 + git.
#
# NO TICKET ID IS STAMPED HERE ON PURPOSE: this gate was built from a
# measurement report, not from a ticket, and D1 gate was already taken by an
# unrelated one. The rule owner is the d1-gate-build report (§a), whose source
# measurement is the two-axis drift report, §3-2 and §5-3. Both are canonical
# build reports and are cited by name on purpose: this file ships publicly and
# must not spell an internal report path.
#
# THE RULE, stated once (D1, operational form):
#
#   An install/publish that carries pack_version V from a pack source that
#   lives INSIDE a git work tree is admitted only when some commit reachable
#   from ANY ref of that work tree's repository holds a pack-manifest.json
#   (at the source's own path inside the repo) whose "pack_version" equals V.
#   No such commit -> REFUSE.
#
#   Read the four terms literally, they are all load-bearing:
#     "carries V"      -- V is the value the caller will WRITE to the live
#                         instance (pack_install.sh's $PACK_VERSION: catalog
#                         row first, manifest fallback), not whatever the file
#                         says right now.
#     "any ref"        -- refs/heads, refs/remotes, refs/tags, i.e. `--all`.
#                         Being on an unmerged branch is enough. This gate is
#                         about REPRODUCIBILITY, not about release lanes.
#     "some commit"    -- history, not tips. A superseded version stays
#                         admitted forever; git can still produce those bytes.
#     "inside a git
#      work tree"      -- the narrowing. See NARROWING below.
#
#   What it deliberately does NOT check: whether the manifest is dirty in some
#   OTHER field, whether the payload matches the committed payload, whether a
#   TAG exists. Those are different predicates with different (much worse)
#   measured false-positive rates -- see WHY, and drift-two-axis.md §3-2.
#
# WHY THIS FILE EXISTS (measurement, not taste). Replay of the 49 installs
# that passed the install gate 2026-09-01..09-09 (d1-gate-build report;
# source: the two-axis drift report, §3-2):
#
#     predicate                          fires   true   false   FP rate
#     D2 "no tag for this version"       24/49      6      18      75%
#     D1 "no commit for this version"     6/49      6       0       0%
#
#   The 6 D1 fires are one event (E11): kit_life 2.6.0 went live on two
#   instances at 09:48 while the value that named it existed only in a working
#   tree; its first commit landed 70 minutes later. For those 70 minutes the
#   live estate held bytes with NO reproduction path -- reverting the working
#   tree would have destroyed the only copy. That is the damage class this
#   gate exists for, and it is exactly the line D1 draws: D2's 18 false
#   positives were all versions that WERE committed and merely untagged, i.e.
#   fully reproducible by SHA.
#
#   Precedent for the shape: the framework release script's first gate already
#   refuses a cut on a dirty tree ("commit or stash everything first"). The
#   framework's own irreversible door has required a commit since DGN-1211;
#   the pack door -- the only door that can carry uncommitted bytes to a live
#   instance at all (pack_install.sh reads $PACKAGE_DIR/pack-manifest.json
#   from the working tree, and neither it nor compat-lint.sh looked at git
#   state anywhere: measured 0 hits) -- did not. This file is that gate moved,
#   not a new mechanism.
#
# NARROWING (measured exception, fail-closed everywhere else). The order was
# fail-CLOSED on "cannot judge". Measured first: a pack source that is NOT in
# a git work tree is a REAL and legitimate install path -- the entire test
# estate uses it. scripts/tests/test-pack-install-{kit,behavior,sharing,
# requires-kit}.sh, test-pack-skill-collision.sh, test-verdict-crosscheck.sh,
# test-pack-deps-provision.sh and tests/rehearsal_dgn227.sh all synthesize a
# fixture pack under $TMPDIR and install it with --pack-dir; none of those
# fixtures is (or should be) a git repository. Refusing them would not stop a
# defect, it would only delete the harness.
#   So the predicate is SCOPED, not weakened: "not under git at all" is
#   NOT-APPLICABLE (SKIP, printed loudly, never silent). Everything inside the
#   scope is fail-closed -- if the source IS in a work tree and any git
#   command fails, the verdict is BLOCK, not SKIP.
#   Measured cost of the scope hole: all three real catalog rows (kit_life,
#   pack_health, dev) resolve to sibling GIT checkouts, so every non-harness
#   install in the estate is inside the scope. A pack copied out of git to
#   dodge the gate is not covered -- stated, not hidden.
#
# NO ESCAPE HATCH. There is deliberately no --force / DOGANY_SKIP_D1 switch,
# for the same reason release.sh states in its own header ("There is
# deliberately NO --force escape hatch. Fix the gate, not the gate."), plus
# one measured reason of its own: the fix for a BLOCK is `git commit`, which
# is seconds of work, always available, and is the exact action the estate
# wanted anyway. A switch would be used at the one moment the gate is right.
#
# COST: one `git log --pickaxe-regex -S ... -- <manifest>` (measured 0.02s hit
# / 0.06s miss on dogany-lifekit 184 commits and on dogany-agent 1558
# commits), plus -- only on the miss path, so only when an install is about to
# be refused -- an exact per-commit JSON re-check of the commits that touched
# that manifest (34 on dogany-lifekit). The re-check exists so that a
# formatting variant of the manifest can never produce a FALSE refusal: the
# regex may only ACQUIT, never convict on its own.

# DGN-1424: shared python3 resolver (PATH-independent -- see the lib file
# header for why a bare `python3` call is unsafe on a human's own terminal).
_PY_RESOLVE_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/py_resolve.sh"
# shellcheck source=py_resolve.sh
. "$_PY_RESOLVE_LIB"

# pack_d1_verdict <manifest_path> <pack_version>
#   Prints exactly one line: "<PASS|SKIP|BLOCK>|<human detail>".
#   Never exits, never writes, never touches the repository it inspects.
pack_d1_verdict() {
  local manifest="${1:-}" version="${2:-}"
  if [ -z "$manifest" ] || [ -z "$version" ]; then
    printf 'BLOCK|internal: pack_d1_verdict needs <manifest> <version> (got manifest=%s version=%s)\n' "${manifest:-<empty>}" "${version:-<empty>}"
    return 0
  fi
  if [ ! -f "$manifest" ]; then
    printf 'BLOCK|manifest not readable: %s\n' "$manifest"
    return 0
  fi
  # 'unversioned' is the installer's own legacy marker for a pack that makes
  # no version claim (pack_install.sh: catalog row without pack_version and
  # manifest without one). There is no value to look for, so there is nothing
  # to judge -- SKIP, loudly. It is already loud in the install ledger header.
  if [ "$version" = "unversioned" ]; then
    printf 'SKIP|pack declares no version (unversioned) -- D1 has no value to look for\n'
    return 0
  fi

  local dir top prefix rel
  dir="$(cd "$(dirname "$manifest")" 2>/dev/null && pwd)" || {
    printf 'BLOCK|cannot resolve pack source directory for %s\n' "$manifest"
    return 0
  }
  # The ONLY fail-open branch in this file, and it is the measured NARROWING
  # above: no work tree => predicate not applicable.
  if ! top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" || [ -z "$top" ]; then
    printf 'SKIP|pack source is not inside a git work tree (%s) -- D1 not applicable (test fixture / unpacked payload lane)\n' "$dir"
    return 0
  fi
  if ! prefix="$(git -C "$dir" rev-parse --show-prefix 2>/dev/null)"; then
    printf 'BLOCK|git work tree at %s but `git rev-parse --show-prefix` failed -- cannot judge, fail-closed\n' "$top"
    return 0
  fi
  rel="${prefix}$(basename "$manifest")"

  # DGN-1424: PATH-independent resolution (see py_resolve.sh header). A
  # crashed predicate (no suitable python3, killed git) must produce a BLOCK
  # with a printed reason, never a bare set -e death -- same doctrine as the
  # git failures above, so a missing/too-old interpreter is treated no
  # differently from a missing git.
  pack_resolve_py3 || { printf 'BLOCK|no Python 3.11+ interpreter available -- cannot judge, fail-closed\n'; return 0; }

  "$PACK_PY3" - "$top" "$rel" "$version" <<'PYEOF'
import json, re, subprocess, sys

top, rel, want = sys.argv[1], sys.argv[2], sys.argv[3]


def git(args):
    return subprocess.run(["git", "-C", top] + args,
                          capture_output=True, text=True)


def out(verdict, detail):
    print("%s|%s" % (verdict, detail))
    sys.exit(0)


# Stage 1 -- pickaxe. `-S <re> --pickaxe-regex -- <manifest>` finds a commit
# where the number of occurrences of `"pack_version": "<want>"` CHANGED, i.e.
# the commit that introduced (or removed) that exact declaration. One walk,
# newest-first, stops at the first hit. This stage may only ACQUIT.
pat = r'"pack_version"[ \t]*:[ \t]*"%s"' % re.escape(want)
r = git(["log", "--all", "-1", "--format=%H", "--pickaxe-regex", "-S", pat,
         "--", rel])
if r.returncode != 0:
    err = (r.stderr or "").strip().splitlines()
    out("BLOCK", "git log failed in %s (%s) -- cannot judge, fail-closed"
        % (top, err[0] if err else "no stderr"))
sha = r.stdout.strip().split("\n")[0].strip()
if sha:
    # NB: -S fires on the commit where the OCCURRENCE COUNT changed, which is
    # either the commit that introduced the declaration or the one that
    # superseded it. Both answers mean the same thing for D1 -- a committed
    # tree carries V (in the supersede case, that commit's parent) -- so both
    # are a PASS. The SHA is printed as "the commit that names it", not as
    # "the commit that has it".
    out("PASS", "pack_version=%s is committed -- %s is where that declaration "
                "changes in %s" % (want, sha[:12], rel))

# Stage 2 -- exact re-check, paid ONLY when stage 1 missed (i.e. only when we
# are about to refuse an install). Parses every historical version of the
# manifest as JSON, so a reformatted manifest can never cause a FALSE refusal.
r = git(["rev-list", "--all", "--", rel])
if r.returncode != 0:
    out("BLOCK", "git rev-list failed in %s -- cannot judge, fail-closed" % top)
shas = r.stdout.split()
if not shas:
    out("BLOCK", "no commit on any ref of %s has ever carried %s -- the "
                 "manifest itself is uncommitted" % (top, rel))
for s in shas:
    b = git(["show", "%s:%s" % (s, rel)])
    if b.returncode != 0:
        continue
    try:
        v = json.loads(b.stdout).get("pack_version")
    except ValueError:
        continue
    if isinstance(v, str) and v == want:
        out("PASS", "pack_version=%s is committed (%s carries it in %s; "
                    "found by exact re-check, not by pickaxe)"
            % (want, s[:12], rel))

newest = "?"
try:
    newest = json.loads(git(["show", "%s:%s" % (shas[0], rel)]).stdout).get("pack_version") or "?"
except ValueError:
    pass
out("BLOCK", "pack_version=%s has NO commit on any ref of %s (%d commit(s) touch "
             "%s; the newest committed value is %s)"
    % (want, top, len(shas), rel, newest))
PYEOF
}

# pack_d1_tag_ref <pack_id> <pack_version>
#   The estate's pack tag name for (id, version). One spelling, one place.
#   Grammar = dec-120 #1 (kit-life tag grammar worklog, 2026-08-28,
#   namespace-decision): the prefix follows the PACK ID, not the repo name,
#   because the auditor (declared-actual-watch) reads it as the id. Measured
#   against the live tag sets before being written down here: dogany-lifekit
#   carries pack/kit_life/v2.0.0..v2.9.0 (and the pre-rename pack/lifekit/*
#   series, deliberately left alone), dogany-agentpack-health-trainer carries
#   pack/health-trainer/v0.1.0..v0.4.1, dogany-agentpack-dev carries
#   pack/dev/v1.3.0..v1.3.1.
pack_d1_tag_ref() {
  printf 'pack/%s/v%s\n' "${1:-<pack-id>}" "${2:-<version>}"
}
