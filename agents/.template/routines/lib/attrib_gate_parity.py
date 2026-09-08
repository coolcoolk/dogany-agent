#!/usr/bin/env python3
"""Parity check between attrib_gate copies (DGN-1331, debt from DGN-1323).

WHY BY (ROLE, PATTERN BODY) AND NOT BY NAME
  The 2026-09-07 A3-iii(c) amendment: "a rule spanning copies has a single site
  or a machine comparison". The first comparison anyone reaches for is the name
  list. Measured on two real copies, the name list is WRONG IN BOTH DIRECTIONS:

    phantom  SEPARATED + READING  vs  FIELDS
             Same two roles, different packaging (two constants vs one tuple).
             Name comparison reports 3 diffs; the real role-level count is 0.
    missed   one copy's reading predicate accepts instance-language seam
             markers, the other's does not. Same role, same name-level verdict
             ("both sides have a reading predicate"), DIFFERENT BEHAVIOUR: a
             document marked in the instance language passes one direction and
             blocks the other.

  So the comparison unit is locked as (role, pattern body). Names are packaging.

WHAT THIS DOES NOT DO
  It cannot decide REGEX SEMANTIC EQUIVALENCE (DGN-1331 lists this as an open
  gap). When two bodies differ textually it does not assert they differ in
  behaviour: it runs a probe corpus and reports BEHAVIOURAL only when it holds a
  discriminating input in hand. Otherwise it says TEXTUAL and says so plainly --
  "no discriminating probe found" is not a proof of equivalence.

FLEET NOTE (ported to agents/.template 2026-09-08)
  The original single-instance version hardcoded a sibling path to one named
  peer instance and labelled its two sides by their instance slugs. On any
  other instance that path does not exist and the tool reported a divergence
  it had never measured.

  This copy is instance-agnostic:
    - LOCAL  = the attrib_gate.py next to this file.
    - PEER   = --peer PATH, else $DOGANY_PEER_ATTRIB_GATE, else every sibling
               instance directory that carries routines/lib/attrib_gate.py.
    - No peer found is a SCAN-FAIL (exit 2), never a silent pass and never a
      false divergence report. A single-copy instance is the normal case: there
      is nothing to compare, and saying so is the honest output.

READ-ONLY. Writes nothing. Reports to stdout.
  exit 0 = no behavioural divergence found (or nothing to compare after a
           successful comparison run)
  exit 1 = divergence (behavioural, missing role, or unmapped predicate)
  exit 2 = SCAN-FAIL: no peer copy to compare against
Not wired to any hook; run it by hand at landing time.
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DEFAULT = os.path.join(HERE, "attrib_gate.py")
TABLE_DEFAULT = os.path.join(HERE, "claim_source_table.py")

# routines/lib -> routines -> instance root
INSTANCE_ROOT = os.path.dirname(os.path.dirname(HERE))
INSTANCE_PARENT = os.path.dirname(INSTANCE_ROOT)

# (constant name as written) -> role. Roles are the comparison unit.
ROLE_MAP = {
    "CLAIM": "owner-decision-collocation",
    "SUMMARY": "owner-answered-summary",
    "SEPARATED": "field:owner-verbatim",
    "READING": "field:agent-reading",
    "FIELDS[0]": "field:owner-verbatim",
    "FIELDS[1]": "field:agent-reading",
    "TIME": "time-marker",
    "UTTER": "utterance-word",
    "CONFESS": "confession-exclusion",
}

# Inputs that discriminate between the known surface forms. Extend when a new
# predicate lands -- a body diff with no discriminating probe is reported as
# TEXTUAL, never silently as equivalent.
PROBE = [
    "owner-verbatim: 형님이 한 말",
    "metal-reading: 내가 읽은 것",  # estate-scrub:allow(discriminator probe fixture -- real agent-slug input the READING regex must match)
    "skull-reading: 내가 읽은 것",  # estate-scrub:allow(discriminator probe fixture -- real agent-slug input the READING regex must match)
    "warg-reading: 내가 읽은 것",  # estate-scrub:allow(discriminator probe fixture -- real agent-slug input the READING regex must match)
    "agent-reading: 내가 읽은 것",
    "내 읽기: 이것은 승인이다",
    "나는 이것을 승인으로 읽는다",
    "형님이 이미 답했다",
    "형님께서 벌써 정하셨다",
    "형님이 확정했다",
    "형님 지시로 확정",
    "오너 승인 축자",
    "네 해석을 형님 확정으로 승계했다",
    "내가 틀렸다",
    "확인 안 했다",
    "2026-09-07 11:20",
    "11:20",
    "축자 원문 발화",
    "said verbatim",
    "reading:",
    "형님은 판정했다",
    "형님이 결론냈다",
    "형님이 말씀하셨다",
]


def _pattern_body(node):
    """Pattern string of a re.compile(...) call node, else None.

    Adjacent string literals are folded by the parser, so implicit concatenation
    across source lines arrives as one constant.
    """
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if not (isinstance(fn, ast.Attribute) and fn.attr == "compile"):
        return None
    if not node.args:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def extract(path):
    """[(name, body)] for every re.compile assignment at ANY depth.

    Depth matters: a copy may keep CONFESS as a main()-local. A module-level-only
    walk would report it as absent on both sides -- the exact phantom this
    checker exists to prevent.
    """
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            body = _pattern_body(node.value)
            if body is not None:
                found.append((target.id, body))
            elif isinstance(node.value, ast.Tuple):
                for i, elt in enumerate(node.value.elts):
                    b = _pattern_body(elt)
                    if b is not None:
                        found.append(("%s[%d]" % (target.id, i), b))
    return found


def module_level_names(path):
    """Names assigned at module scope -- for the 'module-level' half of the rule."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def discover_peers(local_path):
    """Sibling instances that carry their own attrib_gate.py. [] = none.

    An instance lives at <parent>/<name>/, so the peers are the other entries of
    <parent> that have routines/lib/attrib_gate.py. Self is excluded by real
    path, not by name, so a symlinked or worktree checkout does not compare
    against itself.
    """
    peers = []
    local_real = os.path.realpath(local_path)
    if not os.path.isdir(INSTANCE_PARENT):
        return peers
    try:
        entries = sorted(os.listdir(INSTANCE_PARENT))
    except OSError:
        return peers
    for name in entries:
        cand = os.path.join(INSTANCE_PARENT, name,
                            "routines", "lib", "attrib_gate.py")
        if not os.path.isfile(cand):
            continue
        if os.path.realpath(cand) == local_real:
            continue
        peers.append(cand)
    return peers


def discriminate(body_a, body_b):
    """Probe inputs where the two patterns disagree. [] = none found."""
    try:
        ra, rb = re.compile(body_a, re.I), re.compile(body_b, re.I)
    except re.error as exc:                                # pragma: no cover
        return ["<compile error: %s>" % exc]
    return [p for p in PROBE if bool(ra.search(p)) != bool(rb.search(p))]


def by_role(pairs):
    out = {}
    for name, body in pairs:
        role = ROLE_MAP.get(name)
        if role is None:
            out.setdefault("UNMAPPED:" + name, []).append((name, body))
        else:
            out.setdefault(role, []).append((name, body))
    return out


def compare(local_path, peer_path):
    """Print one local-vs-peer report. Returns True when divergence was found."""
    l_pairs, p_pairs = extract(local_path), extract(peer_path)
    l_mod, p_mod = module_level_names(local_path), module_level_names(peer_path)

    # ---- layer 1: the known-bad method, shown so the phantoms are visible ----
    # Bare identifiers AS WRITTEN in the source -- that is what a name-list
    # comparison actually sees. A copy that packs two roles into ONE name (a
    # FIELDS tuple) must stay unindexed here, or the packaging difference this
    # layer exists to expose disappears.
    l_names = {n.split("[")[0] for n, _ in l_pairs}
    p_names = {n.split("[")[0] for n, _ in p_pairs}
    l_only = sorted(l_names - p_names)
    p_only = sorted(p_names - l_names)
    print("\n[1] NAME-LEVEL COMPARISON (known-bad -- shown for contrast)")
    print("    local names: %s" % ", ".join(sorted(l_names)))
    print("    peer  names: %s" % ", ".join(sorted(p_names)))
    print("    local-only : %s" % (", ".join(l_only) or "-"))
    print("    peer-only  : %s" % (", ".join(p_only) or "-"))
    print("    name-level diff count: %d" % (len(l_only) + len(p_only)))

    # ---- layer 2: the locked unit ------------------------------------------ #
    l_roles, p_roles = by_role(l_pairs), by_role(p_pairs)
    print("\n[2] ROLE-LEVEL COMPARISON (locked unit)")
    real, unmapped, missing, phantom = [], [], [], []
    for role in sorted(set(l_roles) | set(p_roles)):
        if role.startswith("UNMAPPED:"):
            side = "local" if role in l_roles else "peer"
            print("    UNMAPPED   %-26s (%s) -- add it to ROLE_MAP" %
                  (role.split(":", 1)[1], side))
            unmapped.append(role)
            continue
        ll, pp = l_roles.get(role), p_roles.get(role)
        if not ll or not pp:
            side = "local" if ll else "peer"
            print("    MISSING    %-26s present only in %s" % (role, side))
            missing.append(role)
            continue
        ln, lb = ll[0]
        pn, pb = pp[0]
        if lb == pb:
            tag = "EQUIVALENT" if ln != pn else "SAME"
            print("    %-10s %-26s local:%-12s peer:%-12s (identical body)" %
                  (tag, role, ln, pn))
            if ln != pn:
                phantom.append((role, ln, pn))
            continue
        probes = discriminate(lb, pb)
        kind = "BEHAVIOURAL" if probes else "TEXTUAL"
        print("    DIFF/%-6s %-26s local:%-12s peer:%-12s" % (kind, role, ln, pn))
        print("               local body: %s" % lb)
        print("               peer  body: %s" % pb)
        if probes:
            for p in probes[:4]:
                la = bool(re.compile(lb, re.I).search(p))
                print("               discriminating input: %-32r local=%s peer=%s"
                      % (p, "MATCH" if la else "no", "no" if la else "MATCH"))
            real.append((role, ln, pn, probes))
        else:
            print("               no discriminating probe found -- "
                  "NOT a proof of equivalence (open gap, DGN-1331)")

    print("\n[3] PHANTOM DIFFS RESOLVED (what the name list got wrong)")
    name_diffs = sorted(l_only) + sorted(p_only)
    if name_diffs:
        for n in name_diffs:
            roles_here = sorted({ROLE_MAP.get(k, "UNMAPPED")
                                 for k in ROLE_MAP if k.split("[")[0] == n})
            print("    name-diff %-12s -> role(s): %s"
                  % (n, ", ".join(roles_here) or "UNMAPPED"))
        print("    -> %d name-level diffs collapse to %d role(s); "
              "%d EQUIVALENT (packaging only), %d genuinely different bodies."
              % (len(name_diffs),
                 len({r for n in name_diffs for k, r in ROLE_MAP.items()
                      if k.split("[")[0] == n}),
                 len(phantom),
                 len([r for r in real if r[0] in
                      {rr for n in name_diffs for k, rr in ROLE_MAP.items()
                       if k.split("[")[0] == n}])))
        print("       A name-only comparison would have reported all %d as "
              "divergence AND missed any body diff entirely." % len(name_diffs))
    else:
        print("    none")

    print("\n[4] STRUCTURE RULE (module-level + enumerable name)")
    for side, pairs, mod in (("local", l_pairs, l_mod), ("peer", p_pairs, p_mod)):
        locals_ = sorted({n.split("[")[0] for n, _ in pairs} - mod)
        print("    %-5s non-module-level predicates: %s"
              % (side, ", ".join(locals_) or "-"))

    print("\n  VERDICT for this peer")
    print("    real behavioural diffs : %d" % len(real))
    for role, ln, pn, probes in real:
        print("      - %s (local %s vs peer %s), e.g. %r" % (role, ln, pn, probes[0]))
    print("    phantom (name-only)    : %d" % len(phantom))
    print("    role missing one side  : %d" % len(missing))
    print("    unmapped predicates    : %d" % len(unmapped))
    return bool(real or missing or unmapped)


def print_table():
    print("\n[5] DGN-1331 TABLE (claim_source_table.py -- this instance)")
    if not os.path.exists(TABLE_DEFAULT):
        print("    table not found next to this checker")
        return
    sys.path.insert(0, HERE)
    import claim_source_table as tbl
    for role, slot, name, _body in tbl.inventory():
        print("    local  %-14s %-9s %s" % (role, slot, name))
    print("    -> %d predicates, %d roles. A peer copy that lacks the table "
          "reads its own claim types from nowhere." % (len(tbl.inventory()),
                                                       len(tbl.ROLES)))


def main(argv):
    args = list(argv)
    local_path = LOCAL_DEFAULT
    peer_args = []
    if "--local" in args:
        local_path = args[args.index("--local") + 1]
    while "--peer" in args:
        i = args.index("--peer")
        peer_args.append(args[i + 1])
        del args[i:i + 2]
    # Legacy flag/env names from the two-instance original, kept so an existing
    # caller does not silently stop comparing.
    if "--metal" in args:  # estate-scrub:allow(legacy CLI flag name -- back-compat, changing it breaks an existing caller)
        local_path = args[args.index("--metal") + 1]  # estate-scrub:allow(legacy CLI flag name -- back-compat, changing it breaks an existing caller)
    if "--skull" in args:  # estate-scrub:allow(legacy CLI flag name -- back-compat, changing it breaks an existing caller)
        peer_args.append(args[args.index("--skull") + 1])  # estate-scrub:allow(legacy CLI flag name -- back-compat, changing it breaks an existing caller)
    env_peer = (os.environ.get("DOGANY_PEER_ATTRIB_GATE")
                or os.environ.get("DGN_SKULL_ATTRIB_GATE"))
    if env_peer and not peer_args:
        peer_args.append(env_peer)

    print("=" * 74)
    print("attrib_gate parity -- DGN-1331 (unit = (role, pattern body))")
    print("=" * 74)
    print("local: %s" % local_path)

    if not os.path.isfile(local_path):
        print("\nSCAN-FAIL: local copy not readable at that path.")
        return 2

    if peer_args:
        peers = [p for p in peer_args]
        missing_peers = [p for p in peers if not os.path.isfile(p)]
        if missing_peers:
            print("\nSCAN-FAIL: peer copy not readable: %s"
                  % ", ".join(missing_peers))
            print("           pass --peer <path> or set DOGANY_PEER_ATTRIB_GATE.")
            return 2
        print("peer source: explicit (--peer / env)")
    else:
        peers = discover_peers(local_path)
        print("peer source: sibling scan under %s" % INSTANCE_PARENT)

    if not peers:
        print("\nSCAN-FAIL: no peer attrib_gate.py found -- nothing to compare.")
        print("           This is the normal state for a single-copy instance.")
        print("           A rule spanning copies needs this check only when a")
        print("           second copy exists; pass --peer <path> to force one.")
        print_table()
        return 2

    for p in peers:
        print("peer : %s" % p)

    diverged = False
    for peer_path in peers:
        print("\n" + "-" * 74)
        print("PEER: %s" % peer_path)
        print("-" * 74)
        if compare(local_path, peer_path):
            diverged = True

    print_table()

    print("\nOVERALL")
    print("  peers compared : %d" % len(peers))
    print("  divergence     : %s" % ("YES" if diverged else "no"))
    return 1 if diverged else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
